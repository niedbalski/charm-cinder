import os
import subprocess

from collections import OrderedDict
from copy import copy

from charmhelpers.core.hookenv import (
    config,
    relation_ids,
    log,
    service_name
)

from charmhelpers.fetch import (
    apt_upgrade,
    apt_update,
    apt_install
)

from charmhelpers.core.host import (
    mounts,
    umount,
    service_stop,
    service_start,
    mkdir
)

from charmhelpers.contrib.storage.linux.ceph import (
    create_pool as ceph_create_pool,
    pool_exists as ceph_pool_exists,
)

from charmhelpers.contrib.openstack.alternatives import install_alternative
from charmhelpers.contrib.hahelpers.cluster import (
    eligible_leader,
)

from charmhelpers.contrib.storage.linux.utils import (
    is_block_device,
    zap_disk,
    is_device_mounted
)

from charmhelpers.contrib.storage.linux.lvm import (
    create_lvm_physical_volume,
    create_lvm_volume_group,
    deactivate_lvm_volume_group,
    is_lvm_physical_volume,
    remove_lvm_physical_volume,
    list_lvm_volume_group
)

from charmhelpers.contrib.storage.linux.loopback import (
    ensure_loopback_device,
)

from charmhelpers.contrib.openstack import (
    templating,
    context,
)

from charmhelpers.contrib.openstack.utils import (
    configure_installation_source,
    get_os_codename_package,
    get_os_codename_install_source,
)

import cinder_contexts

COMMON_PACKAGES = [
    'apache2',
    'cinder-common',
    'gdisk',
    'haproxy',
    'python-jinja2',
    'python-keystoneclient',
    'python-mysqldb',
    'python-psycopg2',
    'qemu-utils',
]

API_PACKAGES = ['cinder-api']
VOLUME_PACKAGES = ['cinder-volume']
SCHEDULER_PACKAGES = ['cinder-scheduler']

DEFAULT_LOOPBACK_SIZE = '5G'

# Cluster resource used to determine leadership when hacluster'd
CLUSTER_RES = 'res_cinder_vip'


class CinderCharmError(Exception):
    pass

CINDER_CONF_DIR = "/etc/cinder"
CINDER_CONF = '%s/cinder.conf' % CINDER_CONF_DIR
CINDER_API_CONF = '%s/api-paste.ini' % CINDER_CONF_DIR
CEPH_CONF = '/etc/ceph/ceph.conf'
CHARM_CEPH_CONF = '/var/lib/charm/{}/ceph.conf'

HAPROXY_CONF = '/etc/haproxy/haproxy.cfg'
APACHE_SITE_CONF = '/etc/apache2/sites-available/openstack_https_frontend'
APACHE_SITE_24_CONF = '/etc/apache2/sites-available/' \
    'openstack_https_frontend.conf'

TEMPLATES = 'templates/'


def ceph_config_file():
    return CHARM_CEPH_CONF.format(service_name())

# Map config files to hook contexts and services that will be associated
# with file in restart_on_changes()'s service map.
CONFIG_FILES = OrderedDict([
    (CINDER_CONF, {
        'hook_contexts': [context.SharedDBContext(ssl_dir=CINDER_CONF_DIR),
                          context.PostgresqlDBContext(),
                          context.AMQPContext(ssl_dir=CINDER_CONF_DIR),
                          context.ImageServiceContext(),
                          context.OSConfigFlagContext(),
                          context.SyslogContext(),
                          cinder_contexts.CephContext(),
                          cinder_contexts.HAProxyContext(),
                          cinder_contexts.ImageServiceContext(),
                          context.SubordinateConfigContext(
                              interface='storage-backend',
                              service='cinder',
                              config_file=CINDER_CONF),
                          cinder_contexts.StorageBackendContext(),
                          cinder_contexts.LoggingConfigContext(),
                          context.IdentityServiceContext()],
        'services': ['cinder-api', 'cinder-volume',
                     'cinder-scheduler', 'haproxy']
    }),
    (CINDER_API_CONF, {
        'hook_contexts': [context.IdentityServiceContext()],
        'services': ['cinder-api'],
    }),
    (ceph_config_file(), {
        'hook_contexts': [context.CephContext()],
        'services': ['cinder-volume']
    }),
    (HAPROXY_CONF, {
        'hook_contexts': [context.HAProxyContext(),
                          cinder_contexts.HAProxyContext()],
        'services': ['haproxy'],
    }),
    (APACHE_SITE_CONF, {
        'hook_contexts': [cinder_contexts.ApacheSSLContext()],
        'services': ['apache2'],
    }),
    (APACHE_SITE_24_CONF, {
        'hook_contexts': [cinder_contexts.ApacheSSLContext()],
        'services': ['apache2'],
    }),
])


def register_configs():
    """Register config files with their respective contexts.
    Regstration of some configs may not be required depending on
    existing of certain relations.
    """
    # if called without anything installed (eg during install hook)
    # just default to earliest supported release. configs dont get touched
    # till post-install, anyway.
    release = get_os_codename_package('cinder-common', fatal=False) or 'folsom'
    configs = templating.OSConfigRenderer(templates_dir=TEMPLATES,
                                          openstack_release=release)

    confs = [CINDER_API_CONF,
             CINDER_CONF,
             HAPROXY_CONF]

    if relation_ids('ceph'):
        # need to create this early, new peers will have a relation during
        # registration # before they've run the ceph hooks to create the
        # directory.
        mkdir(os.path.dirname(CEPH_CONF))
        mkdir(os.path.dirname(ceph_config_file()))

        # Install ceph config as an alternative for co-location with
        # ceph and ceph-osd charm - cinder ceph.conf will be
        # lower priority than both of these but thats OK
        if not os.path.exists(ceph_config_file()):
            # touch file for pre-templated generation
            open(ceph_config_file(), 'w').close()
        install_alternative(os.path.basename(CEPH_CONF),
                            CEPH_CONF, ceph_config_file())
        confs.append(ceph_config_file())

    for conf in confs:
        configs.register(conf, CONFIG_FILES[conf]['hook_contexts'])

    if os.path.exists('/etc/apache2/conf-available'):
        configs.register(APACHE_SITE_24_CONF,
                         CONFIG_FILES[APACHE_SITE_24_CONF]['hook_contexts'])
    else:
        configs.register(APACHE_SITE_CONF,
                         CONFIG_FILES[APACHE_SITE_CONF]['hook_contexts'])
    return configs


def juju_log(msg):
    log('[cinder] %s' % msg)


def determine_packages():
    '''Determine list of packages required for the currently enabled services.

    :returns: list of package names
    '''
    pkgs = copy(COMMON_PACKAGES)
    for s, p in [('api', API_PACKAGES),
                 ('volume', VOLUME_PACKAGES),
                 ('scheduler', SCHEDULER_PACKAGES)]:
        if service_enabled(s):
            pkgs += p
    return pkgs


def service_enabled(service):
    '''Determine if a specific cinder service is enabled in
    charm configuration.

    :param service: str: cinder service name to query (volume, scheduler, api,
                         all)

    :returns: boolean: True if service is enabled in config, False if not.
    '''
    enabled = config()['enabled-services']
    if enabled == 'all':
        return True
    return service in enabled


def restart_map():
    '''Determine the correct resource map to be passed to
    charmhelpers.core.restart_on_change() based on the services configured.

    :returns: dict: A dictionary mapping config file to lists of services
                    that should be restarted when file changes.
    '''
    _map = []
    for f, ctxt in CONFIG_FILES.iteritems():
        svcs = []
        for svc in ctxt['services']:
            if svc.startswith('cinder-'):
                if service_enabled(svc.split('-')[1]):
                    svcs.append(svc)
            else:
                svcs.append(svc)
        if svcs:
            _map.append((f, svcs))
    return OrderedDict(_map)


def services():
    ''' Returns a list of services associate with this charm '''
    _services = []
    for v in restart_map().values():
        _services = _services + v
    return list(set(_services))


def extend_lvm_volume_group(volume_group, block_device):
    '''
    Extend and LVM volume group onto a given block device.

    Assumes block device has already been initialized as an LVM PV.

    :param volume_group: str: Name of volume group to create.
    :block_device: str: Full path of PV-initialized block device.
    '''
    subprocess.check_call(['vgextend', volume_group, block_device])


def configure_lvm_storage(block_devices, volume_group, overwrite=False):
    ''' Configure LVM storage on the list of block devices provided

    :param block_devices: list: List of whitelisted block devices to detect
                                and use if found
    :param overwrite: bool: Scrub any existing block data if block device is
                            not already in-use
    '''
    devices = []
    for block_device in block_devices:
        (block_device, size) = _parse_block_device(block_device)

        if not is_device_mounted(block_device):
            if size == 0 and is_block_device(block_device):
                devices.append(block_device)
            elif size > 0:
                devices.append(ensure_loopback_device(block_device, size))

    # NOTE(jamespage)
    # might need todo an initial one-time scrub on install if need be
    vg_found = False
    new_devices = []
    for device in devices:
        if (not is_lvm_physical_volume(device) or
                (is_lvm_physical_volume(device) and
                 list_lvm_volume_group(device) != volume_group)):
            # Existing LVM but not part of required VG or new device
            if overwrite is True:
                clean_storage(device)
                new_devices.append(device)
                create_lvm_physical_volume(device)
        elif (is_lvm_physical_volume(device) and
                list_lvm_volume_group(device) == volume_group):
            # Mark vg as found
            vg_found = True

    if vg_found is False and len(new_devices) > 0:
        # Create new volume group from first device
        create_lvm_volume_group(volume_group, new_devices[0])
        new_devices.remove(new_devices[0])

    if len(new_devices) > 0:
        # Extend the volume group as required
        for new_device in new_devices:
            extend_lvm_volume_group(volume_group, new_device)


def clean_storage(block_device):
    '''Ensures a block device is clean.  That is:
        - unmounted
        - any lvm volume groups are deactivated
        - any lvm physical device signatures removed
        - partition table wiped

    :param block_device: str: Full path to block device to clean.
    '''
    for mp, d in mounts():
        if d == block_device:
            juju_log('clean_storage(): Found %s mounted @ %s, unmounting.' %
                     (d, mp))
            umount(mp, persist=True)

    if is_lvm_physical_volume(block_device):
        deactivate_lvm_volume_group(block_device)
        remove_lvm_physical_volume(block_device)

    zap_disk(block_device)


def _parse_block_device(block_device):
    ''' Parse a block device string and return either the full path
    to the block device, or the path to a loopback device and its size

    :param: block_device: str: Block device as provided in configuration

    :returns: (str, int): Full path to block device and 0 OR
                          Full path to loopback device and required size
    '''
    _none = ['None', 'none', None]
    if block_device in _none:
        return (None, 0)
    if block_device.startswith('/dev/'):
        return (block_device, 0)
    elif block_device.startswith('/'):
        _bd = block_device.split('|')
        if len(_bd) == 2:
            bdev, size = _bd
        else:
            bdev = block_device
            size = DEFAULT_LOOPBACK_SIZE
        return (bdev, size)
    else:
        return ('/dev/{}'.format(block_device), 0)


def migrate_database():
    'Runs cinder-manage to initialize a new database or migrate existing'
    cmd = ['cinder-manage', 'db', 'sync']
    subprocess.check_call(cmd)


def ensure_ceph_pool(service, replicas):
    'Creates a ceph pool for service if one does not exist'
    # TODO(Ditto about moving somewhere sharable)
    if not ceph_pool_exists(service=service, name=service):
        ceph_create_pool(service=service, name=service, replicas=replicas)


def set_ceph_env_variables(service):
    # XXX: Horrid kludge to make cinder-volume use
    # a different ceph username than admin
    env = open('/etc/environment', 'r').read()
    if 'CEPH_ARGS' not in env:
        with open('/etc/environment', 'a') as out:
            out.write('CEPH_ARGS="--id %s"\n' % service)
    with open('/etc/init/cinder-volume.override', 'w') as out:
            out.write('env CEPH_ARGS="--id %s"\n' % service)


def do_openstack_upgrade(configs):
    """Perform an uprade of cinder. Takes care of upgrading
    packages, rewriting configs + database migration and
    potentially any other post-upgrade actions.

    :param configs: The charms main OSConfigRenderer object.

    """
    new_src = config('openstack-origin')
    new_os_rel = get_os_codename_install_source(new_src)

    juju_log('Performing OpenStack upgrade to %s.' % (new_os_rel))

    configure_installation_source(new_src)
    dpkg_opts = [
        '--option', 'Dpkg::Options::=--force-confnew',
        '--option', 'Dpkg::Options::=--force-confdef',
    ]
    apt_update()
    apt_upgrade(options=dpkg_opts, fatal=True, dist=True)
    apt_install(determine_packages(), fatal=True)

    # set CONFIGS to load templates from new release and regenerate config
    configs.set_release(openstack_release=new_os_rel)
    configs.write_all()

    # Stop/start services and migrate DB if leader
    [service_stop(s) for s in services()]
    if eligible_leader(CLUSTER_RES):
        migrate_database()
    [service_start(s) for s in services()]