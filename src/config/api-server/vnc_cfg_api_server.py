#
# Copyright (c) 2013 Juniper Networks, Inc. All rights reserved.
#
"""
This is the main module in vnc_cfg_api_server package. It manages interaction
between http/rest, address management, authentication and database interfaces.
"""

from gevent import monkey; monkey.patch_all()
from gevent import hub

import sys
import logging
import signal
import os
import socket
import json
import uuid
import copy
import argparse
import ConfigParser
from pprint import pformat

"""
Following is needed to silence warnings on every request when keystone auth_token
middleware + Sandesh is used. Keystone or Sandesh alone do not produce these warnings.

Exception AttributeError: AttributeError("'_DummyThread' object has no attribute 
'_Thread__block'",) in <module 'threading' from '/usr/lib64/python2.7/threading.pyc'> 
ignored

See http://stackoverflow.com/questions/13193278/understand-python-threading-bug
for more information.
"""
import threading
threading._DummyThread._Thread__stop = lambda x: 42

CONFIG_VERSION = '1.0'

import bottle

import vnc_cfg_types
from vnc_cfg_ifmap import VncDbClient

from cfgm_common.uve.vnc_api.ttypes import VncApiCommon, VncApiReadLog, VncApiConfigLog, VncApiError
from cfgm_common.uve.virtual_machine.ttypes import VMLog
from cfgm_common.uve.virtual_network.ttypes import UveVirtualNetworkConfig, UveVirtualNetworkConfigTrace, VnPolicy, VNLog
from cfgm_common.uve.vrouter.ttypes import VRLog
from cfgm_common.sandesh.vns.ttypes import Module
from cfgm_common.sandesh.vns.constants import ModuleNames 
from provision_defaults import Provision
from gen.resource_xsd import *
from gen.resource_common import *
from gen.resource_server import *
from gen.vnc_api_server_gen import VncApiServerGen
import cfgm_common
from cfgm_common.rest import LinkObject
from cfgm_common.exceptions import *
from cfgm_common.vnc_extensions import ExtensionManager, ApiHookManager
import vnc_addr_mgmt
import vnc_auth
import vnc_auth_keystone
import vnc_perms
from cfgm_common import vnc_cpu_info

from pysandesh.sandesh_base import *
from pysandesh.gen_py.sandesh.ttypes import SandeshLevel
import discovery.client as client
#from gen_py.vnc_api.ttypes import *
import netifaces

_WEB_HOST = '0.0.0.0'
_WEB_PORT = 8082

_ACTION_RESOURCES = [
    {'uri': '/fqname-to-id', 'link_name': 'name-to-id',
     'method_name': 'fq_name_to_id_http_post'},
    {'uri': '/id-to-fqname', 'link_name': 'id-to-name',
     'method_name': 'id_to_fq_name_http_post'},
    # ifmap-to-id only for ifmap subcribers using rest for publish
    {'uri': '/ifmap-to-id', 'link_name': 'ifmap-to-id',
     'method_name': 'ifmap_to_id_http_post'},
    {'uri': '/useragent-kv', 'link_name': 'useragent-keyvalue',
     'method_name': 'useragent_kv_http_post'},
    {'uri': '/db-check', 'link_name': 'database-check',
     'method_name': 'db_check'},
    {'uri': '/fetch-records', 'link_name': 'fetch-records',
     'method_name': 'fetch_records'},
]

@bottle.error(403)
def error_403(err):
    return err.body
#end error_403

@bottle.error(404)
def error_404(err):
    return err.body
#end error_404

@bottle.error(409)
def error_409(err):
    return err.body
#end error_409

@bottle.error(500)
def error_500(err):
    return err.body
#end error_500

@bottle.error(503)
def error_503(err):
    return err.body
#end error_503

class VncApiServer(VncApiServerGen):
    """
    This is the manager class co-ordinating all classes present in the package
    """
    def __new__(cls, *args, **kwargs):
        obj = super(VncApiServer, cls).__new__(cls, *args, **kwargs)
        bottle.route('/', 'GET', obj.homepage_http_get)
        for act_res in _ACTION_RESOURCES:
            method = getattr(obj, act_res['method_name'])
            bottle.route(act_res['uri'], 'POST', method)
        return obj
    #end __new__

    def __init__(self, args_str = None):
        #self._init_logging()

        self._args = None
        if not args_str:
            args_str = ' '.join(sys.argv[1:])
        self._parse_args(args_str)

        self._base_url = "http://%s:%s" %(self._args.listen_ip_addr,
                                          self._args.listen_port)
        super(VncApiServer, self).__init__()
        self._pipe_start_app = None

        # REST interface initialization
        self._get_common = self._http_get_common
        self._put_common = self._http_put_common
        self._delete_common = self._http_delete_common
        self._post_common = self._http_post_common

        # Type overrides from generated code
        self._resource_classes['floating-ip'] = vnc_cfg_types.FloatingIpServer
        self._resource_classes['instance-ip'] = vnc_cfg_types.InstanceIpServer
        self._resource_classes['virtual-machine-interface'] = \
                               vnc_cfg_types.VirtualMachineInterfaceServer
        self._resource_classes['virtual-network'] = vnc_cfg_types.VirtualNetworkServer
        self._resource_classes['virtual-DNS'] = vnc_cfg_types.VirtualDnsServer
        self._resource_classes['virtual-DNS-record'] = vnc_cfg_types.VirtualDnsRecordServer

        # TODO default-generation-setting can be from ini file
        self._resource_classes['bgp-router'].generate_default_instance = False
        self._resource_classes['virtual-router'].generate_default_instance = False
        self._resource_classes['access-control-list'].generate_default_instance = False
        self._resource_classes['floating-ip-pool'].generate_default_instance = False
        self._resource_classes['instance-ip'].generate_default_instance = False
        self._resource_classes['virtual-machine'].generate_default_instance = False
        self._resource_classes['virtual-machine-interface'].generate_default_instance = False
        self._resource_classes['service-template'].generate_default_instance = False
        self._resource_classes['service-instance'].generate_default_instance = False
        self._resource_classes['virtual-DNS-record'].generate_default_instance = False
        self._resource_classes['virtual-DNS'].generate_default_instance = False
        self._resource_classes['global-vrouter-config'].generate_default_instance = False

        for act_res in _ACTION_RESOURCES:
            link = LinkObject('action', self._base_url , act_res['uri'],
                              act_res['link_name'])
            self._homepage_links.append(link)

        bottle.route('/documentation/<filename:path>', 'GET', self.documentation_http_get)
        self._homepage_links.insert(0, LinkObject('documentation',
            self._base_url , '/documentation/index.html', 'documentation'))

        # APIs to reserve/free block of IP address from a VN/Subnet
        bottle.route('/virtual-network/<id>/ip-alloc', 'POST', self.vn_ip_alloc_http_post)
        self._homepage_links.append(LinkObject('action',
            self._base_url , '/virtual-network/%s/ip-alloc', 'virtual-network-ip-alloc'))

        bottle.route('/virtual-network/<id>/ip-free',  'POST', self.vn_ip_free_http_post)
        self._homepage_links.append(LinkObject('action',
            self._base_url , '/virtual-network/%s/ip-free', 'virtual-network-ip-free'))

        # APIs to find out number of ip instances from given VN subnet
        bottle.route('/virtual-network/<id>/subnet-ip-count', 'POST', self.vn_subnet_ip_count_http_post)
        self._homepage_links.append(LinkObject('action',
            self._base_url , '/virtual-network/%s/subnet-ip-count', 'virtual-network-subnet-ip-count'))

        # Enable/Disable multi tenancy
        bottle.route('/multi-tenancy', 'GET', self.mt_http_get)
        bottle.route('/multi-tenancy', 'PUT', self.mt_http_put)

        # Initialize discovery client
        self._disc = None
        if self._args.disc_server_ip and self._args.disc_server_port:
            self._disc= client.DiscoveryClient(self._args.disc_server_ip, 
                                               self._args.disc_server_port,
                                               ModuleNames[Module.API_SERVER])

        # sandesh init
        collectors = None
        if self._args.collector and self._args.collector_port:
            collectors = [(self._args.collector, int(self._args.collector_port))]
        self._sandesh = Sandesh()
        self._sandesh.init_generator(ModuleNames[Module.API_SERVER], 
                socket.gethostname(), collectors, 'vnc_api_server_context', 
                int(self._args.http_server_port),
                ['cfgm_common', 'sandesh'], self._disc)
        self._sandesh.trace_buffer_create(name="VncCfgTraceBuf", size=1000)
        self._sandesh.set_logging_params(enable_local_log = self._args.log_local,
                                         category = self._args.log_category,
                                         level = self._args.log_level,
                                         file = self._args.log_file)

        # Load extensions
        self._extension_mgrs = {}
        self._load_extensions()

        # Address Management interface
        addr_mgmt = vnc_addr_mgmt.AddrMgmt(self)
        vnc_cfg_types.VirtualMachineInterfaceServer.addr_mgmt = addr_mgmt
        vnc_cfg_types.FloatingIpServer.addr_mgmt = addr_mgmt
        vnc_cfg_types.InstanceIpServer.addr_mgmt = addr_mgmt
        vnc_cfg_types.VirtualNetworkServer.addr_mgmt = addr_mgmt
        vnc_cfg_types.InstanceIpServer.manager = self
        self._addr_mgmt = addr_mgmt

        # Authn/z interface
        if self._args.auth == 'keystone':
            auth_svc = vnc_auth_keystone.AuthServiceKeystone(self, self._args)
        else:
            auth_svc = vnc_auth.AuthService(self, self._args)

        self._pipe_start_app = auth_svc.get_middleware_app()
        if not self._pipe_start_app:
            self._pipe_start_app = bottle.app()
        self._auth_svc = auth_svc

        # API/Permissions check
        self._permissions = vnc_perms.VncPermissions(self, self._args)

        # DB interface initialization
        if self._args.wipe_config:
            self._db_connect(True)
        else:
            self._db_connect(self._args.reset_config)
            self._db_init_entries()

        # recreate subnet operating state from DB
        (ok, vn_fq_names) = self._db_conn.dbe_list('virtual-network')
        for vn_fq_name in vn_fq_names:
            vn_uuid = self._db_conn.fq_name_to_uuid('virtual-network', vn_fq_name)
            (ok, vn_dict) = self._db_conn.dbe_read('virtual-network', {'uuid': vn_uuid})
            self._addr_mgmt.net_create(vn_dict)

        # Cpuinfo interface
        sysinfo_req = True
        config_node_ip = self.get_server_ip()
        cpu_info = vnc_cpu_info.CpuInfo(Module.API_SERVER, sysinfo_req, self._sandesh, 60, config_node_ip)
        self._cpu_info = cpu_info

    #end __init__

    # Public Methods
    def get_args(self):
        return self._args
    #end get_args

    def get_server_ip(self):
        for i in netifaces.interfaces():
            try:
                if netifaces.AF_INET in netifaces.ifaddresses(i):
                    return netifaces.ifaddresses (i)[netifaces.AF_INET][0][
                        'addr']
            except ValueError, e:
                print "Skipping interface %s" % i
        return None
    #end get_server_ip
    
    def get_listen_ip(self):
        return self._args.listen_ip_addr
    #end get_listen_ip

    def get_server_port(self):
        return self._args.listen_port
    #end get_server_port

    def get_pipe_start_app(self):
        return self._pipe_start_app
    #end get_pipe_start_app

    def homepage_http_get(self):
        json_body = {}
        json_links = []
        # strip trailing '/' in url
        url = bottle.request.url[:-1]
        for link in self._homepage_links:
            # strip trailing '/' in url
            json_links.append(
                         {'link': link.to_dict(with_url = url)}
                         )

        json_body = \
            { "href": url,
              "links": json_links
            }

        return json_body
    #end homepage_http_get

    def documentation_http_get(self, filename):
        return bottle.static_file(filename, root='/usr/share/doc/python-vnc_cfg_api_server/build/html')
    #end documentation_http_get

    def fq_name_to_id_http_post(self):
        self._post_common(bottle.request, None, None)
        obj_type = bottle.request.json['type'].replace('-', '_')
        fq_name = bottle.request.json['fq_name']

        try:
            id = self._db_conn.fq_name_to_uuid(obj_type, fq_name)
        except NoIdError:
            bottle.abort(404, 'Name ' + pformat(fq_name) + ' not found')

        return {'uuid': id}
    #end fq_name_to_id_http_post

    def id_to_fq_name_http_post(self):
        self._post_common(bottle.request, None, None)
        fq_name = self._db_conn.uuid_to_fq_name(bottle.request.json['uuid'])
        obj_type = self._db_conn.uuid_to_obj_type(bottle.request.json['uuid'])
        return {'fq_name': fq_name, 'type': obj_type}
    #end id_to_fq_name_http_post

    def ifmap_to_id_http_post(self):
        self._post_common(bottle.request, None, None)
        uuid = self._db_conn.ifmap_id_to_uuid(bottle.request.json['ifmap_id'])
        return {'uuid': uuid}
    #end ifmap_to_id_http_post

    # Enables a user-agent to store and retrieve key-val pair
    # TODO this should be done only for special/quantum plugin
    def useragent_kv_http_post(self):
        self._post_common(bottle.request, None, None)

        oper = bottle.request.json['operation']
        key = bottle.request.json['key']
        val = bottle.request.json.get('value', '')

        # TODO move values to common
        if oper == 'STORE':
            self._db_conn.useragent_kv_store(key, val)
        elif oper == 'RETRIEVE':
            try:
                result = self._db_conn.useragent_kv_retrieve(key)
                return {'value': result}
            except NoUserAgentKey:
                bottle.abort(404, "Unknown User-Agent key " + key)
        elif oper == 'DELETE':
            result = self._db_conn.useragent_kv_delete(key)
        else:
            bottle.abort(404, "Invalid Operation " + oper)

    #end useragent_kv_http_post

    def db_check(self):
        """ Check database for inconsistencies. No update to database """
        check_result = self._db_conn.db_check()

        return {'results': check_result}
    #end db_check

    def fetch_records(self):
        """ Retrieve and return all records """
        result = self._db_conn.db_read()
        return {'results': result}
    #end fetch_records

    # Private Methods
    def _parse_args(self, args_str):
        '''
        Eg. python vnc_cfg_api_server.py --ifmap_server_ip 192.168.1.17
                                         --ifmap_server_port 8443
                                         --ifmap_username test
                                         --ifmap_password test
                                         --cassandra_server_list 10.1.2.3:9160 10.1.2.4:9160
                                         --collector 127.0.0.1
                                         --collector_port 8080
                                         --http_server_port 8090
                                         --listen_ip_addr 127.0.0.1
                                         --listen_port 8082
                                         --log_local 
                                         --log_level SYS_DEBUG
                                         --log_category test
                                         --log_file <stdout>
                                         --disc_server_ip 127.0.0.1
                                         --disc_server_port 5998
                                         [--auth keystone]
                                         [--ifmap_server_loc /home/contrail/source/ifmap-server/]
        '''

        # Source any specified config/ini file
        # Turn off help, so we print all options in response to -h
        conf_parser = argparse.ArgumentParser(add_help = False)

        conf_parser.add_argument("-c", "--conf_file",
                                 help="Specify config file", metavar="FILE")
        args, remaining_argv = conf_parser.parse_known_args(args_str.split())

        defaults = {
            'reset_config': False,
            'wipe_config': False,
            'listen_ip_addr' : _WEB_HOST,
            'listen_port' : _WEB_PORT,
            'ifmap_server_ip' : _WEB_HOST,
            'ifmap_server_port' : "8443",
            'collector' : None,
            'collector_port' : None,
            'http_server_port' : '8084',
            'log_local' : False,
            'log_level' : SandeshLevel.SYS_DEBUG,
            'log_category' : '',
            'log_file' : Sandesh._DEFAULT_LOG_FILE,
            'multi_tenancy': False,
            'disc_server_ip' : None,
            'disc_server_port' : None,
            }
        # ssl options
        secopts = {
            'use_certs': False,
            'keyfile'  : '',
            'certfile' : '',
            'ca_certs' : '',
            'ifmap_certauth_port' : "8444",
            }
        # keystone options
        ksopts = {
            'auth_host'        : '127.0.0.1',
            'auth_protocol'    : 'http',
            'admin_user'       : '',
            'admin_password'   : '',
            'admin_tenant_name': '',
        }

        config = None
        if args.conf_file:
            config = ConfigParser.SafeConfigParser()
            config.read([args.conf_file])
            defaults.update(dict(config.items("DEFAULTS")))
            if 'multi_tenancy' in config.options('DEFAULTS'):
                defaults['multi_tenancy'] = config.getboolean('DEFAULTS', 'multi_tenancy')
            if 'SECURITY' in config.sections() and 'use_certs' in config.options('SECURITY'):
                if config.getboolean('SECURITY', 'use_certs'):
                    secopts.update(dict(config.items("SECURITY")))
            if 'KEYSTONE' in config.sections():
                ksopts.update(dict(config.items("KEYSTONE")))

        # Override with CLI options
        # Don't surpress add_help here so it will handle -h
        parser = argparse.ArgumentParser(
            # Inherit options from config_parser
            parents=[conf_parser],
            # print script description with -h/--help
            description=__doc__,
            # Don't mess with format of description
            formatter_class=argparse.RawDescriptionHelpFormatter,
            )
        defaults.update(secopts)
        defaults.update(ksopts)
        parser.set_defaults(**defaults)

        parser.add_argument("--ifmap_server_ip", help = "IP address of ifmap server")
        parser.add_argument("--ifmap_server_port", help = "Port of ifmap server")

        # TODO should be from certificate
        parser.add_argument("--ifmap_username",
                            help = "Username known to ifmap server")
        parser.add_argument("--ifmap_password",
                            help = "Password known to ifmap server")
        parser.add_argument("--cassandra_server_list",
                            help = "List of cassandra servers in IP Address:Port format",
                            nargs = '+')
        parser.add_argument("--auth", choices = ['keystone'],
                            help = "Type of authentication for user-requests")
        parser.add_argument("--reset_config", action = "store_true",
                            help = "Warning! Destroy previous configuration and create defaults")
        parser.add_argument("--wipe_config", action = "store_true",
                            help = "Warning! Destroy previous configuration")
        parser.add_argument("--listen_ip_addr",
                            help = "IP address to provide service on, default %s" %(_WEB_HOST))
        parser.add_argument("--listen_port",
                            help = "Port to provide service on, default %s" %(_WEB_PORT))
        parser.add_argument("--collector",
                            help = "IP address of VNC collector server")
        parser.add_argument("--collector_port",
                            help = "Port of VNC collector server")
        parser.add_argument("--http_server_port",
                            help = "Port of local HTTP server")
        parser.add_argument("--ifmap_server_loc",
                            help = "Location of IFMAP server")
        parser.add_argument("--log_local", action = "store_true",
                            help = "Enable local logging of sandesh messages")
        parser.add_argument("--log_level",
                            help = "Severity level for local logging of sandesh messages")
        parser.add_argument("--log_category",
                            help = "Category filter for local logging of sandesh messages")
        parser.add_argument("--log_file",
                            help = "Filename for the logs to be written to")
        parser.add_argument("--multi_tenancy", action = "store_true",
                            help = "Validate resource permissions (implies token validation)")
        self._args = parser.parse_args(remaining_argv)
        self._args.config_sections = config
        if type(self._args.cassandra_server_list) is str:
            self._args.cassandra_server_list = self._args.cassandra_server_list.split()
    #end _parse_args

    # sigchld handler is currently not engaged. See comment @sigchld
    def sigchld_handler(self):
        # DB interface initialization
        self._db_connect(reset_config = False)
        self._db_init_entries()
    #end sigchld_handler

    def _load_extensions(self):
        try:
            conf_sections = self._args.config_sections
            self._extension_mgrs['resync'] = ExtensionManager('vnc_cfg_api.resync', 
                                                          api_server_ip = self._args.listen_ip_addr,
                                                          api_server_port = self._args.listen_port,
                                                          conf_sections = conf_sections)
        except Exception as e:
            pass
    #end _load_extensions

    def _db_connect(self, reset_config):
        ifmap_ip = self._args.ifmap_server_ip
        ifmap_port = self._args.ifmap_server_port
        user = self._args.ifmap_username
        passwd = self._args.ifmap_password
        cass_server_list = self._args.cassandra_server_list
        ifmap_loc = self._args.ifmap_server_loc

        db_conn = VncDbClient(self, ifmap_ip, ifmap_port, user, passwd,
                              cass_server_list, reset_config, ifmap_loc)
        self._db_conn = db_conn
    #end _db_connect

    def _ensure_id_perms_present(self, obj_type, obj_dict):
        """
        Called at resource creation to ensure that id_perms is present in obj
        """
        new_id_perms = self._get_default_id_perms(obj_type)

        if (('id_perms' not in obj_dict) or
            (obj_dict['id_perms'] == None)):
            obj_dict['id_perms'] = new_id_perms
            return

        # Start from default and update from obj_dict
        req_id_perms = obj_dict['id_perms']
        if req_id_perms.has_key('enable'):
            new_id_perms['enable'] = req_id_perms['enable']
        if req_id_perms.has_key('description'):
            new_id_perms['description'] = req_id_perms['description']
        # TODO handle perms present in req_id_perms

        obj_dict['id_perms'] = new_id_perms
    #end _ensure_id_perms_present

    def _get_default_id_perms(self, obj_type):
        id_perms = copy.deepcopy(Provision.defaults.perms[obj_type])
        id_perms_json = json.dumps(id_perms, default=lambda o: dict((k, v) for k, v in o.__dict__.iteritems()))
        id_perms_dict = json.loads(id_perms_json)
        return id_perms_dict
    #end _get_default_id_perms

    def _db_init_entries(self):
        # create singleton defaults if they don't exist already in db
        glb_sys_cfg = self._create_singleton_entry(GlobalSystemConfig(autonomous_system = 64512,
                                                                      config_version = CONFIG_VERSION))
        def_domain = self._create_singleton_entry(Domain())
        ip_fab_vn = self._create_singleton_entry(VirtualNetwork(cfgm_common.IP_FABRIC_VN_FQ_NAME[-1]))
        self._create_singleton_entry(RoutingInstance('__default__', ip_fab_vn))
        link_local_vn = self._create_singleton_entry(VirtualNetwork(cfgm_common.LINK_LOCAL_VN_FQ_NAME[-1]))
        self._create_singleton_entry(RoutingInstance('__link_local__', link_local_vn))

        self._db_conn.db_resync()
        try:
            self._extension_mgrs['resync'].map(self._resync_projects)
        except Exception as e:
            pass
    #end _db_init_entries

    def _resync_projects(self, ext):
        ext.obj.resync_projects()
    #end _resync_projects

    def _create_singleton_entry(self, singleton_obj):
        s_obj = singleton_obj
        obj_type = s_obj.get_type()
        method_name = obj_type.replace('-', '_')
        fq_name = s_obj.get_fq_name()

        # create if it doesn't exist yet
        try:
            id = self._db_conn.fq_name_to_uuid(obj_type, fq_name)
        except NoIdError:
            obj_dict = s_obj.__dict__
            obj_dict['id_perms'] = self._get_default_id_perms(obj_type)
            (ok, result) = self._db_conn.dbe_alloc(obj_type, obj_dict)
            obj_ids = result
            self._db_conn.dbe_create(obj_type, obj_ids, obj_dict)
            method = '_%s_create_default_children' %(method_name)
            def_children_method = getattr(self, method)
            def_children_method(s_obj)

        return s_obj
    #end _create_singleton_entry

    def get_db_connection(self):
        return self._db_conn
    #end get_db_connection

    def generate_url(self, obj_type, obj_uuid):
        obj_uri_type = obj_type.replace('_', '-')
        try:
            url_parts = bottle.request.urlparts
            return '%s://%s/%s/%s' %(url_parts.scheme, url_parts.netloc, obj_uri_type, obj_uuid)
        except Exception as e:
            return '%s/%s/%s' %(self._base_url, obj_uri_type, obj_uuid)
    #end generate_url

    def config_object_error(self, id, fq_name_str, obj_type, operation, err_str):
        apiConfig = VncApiCommon(identifier_uuid=str(id))
        apiConfig.operation = operation
        apiConfig.identifier_name = fq_name_str
        if err_str:
            apiConfig.error = "%s:%s" % (obj_type, err_str)
        uveLog = None

        if obj_type == "virtual_machine" or obj_type == "virtual-machine":
            log = VMLog(api_log = apiConfig, sandesh=self._sandesh)
        elif obj_type == "virtual_network" or obj_type == "virtual-network":
            vn_log = UveVirtualNetworkConfig(name = str(id))
            uveLog = UveVirtualNetworkConfigTrace(data = vn_log, sandesh=self._sandesh)
            log = VNLog(api_log = apiConfig, sandesh=self._sandesh)
        elif obj_type == "virtual_router" or obj_type == "virtual-router":
            log = VRLog(api_log = apiConfig, sandesh=self._sandesh)
        else:
            log = VncApiConfigLog(api_log = apiConfig, sandesh=self._sandesh)

        if uveLog:
            uveLog.send(sandesh=self._sandesh)
        log.send(sandesh=self._sandesh)
    #end config_object_error

    def config_log_error(self, err_str):
        VncApiError(api_error_msg = err_str, sandesh=self._sandesh).send(sandesh=self._sandesh)
    #end config_log_error

    def add_virtual_network_refs(self, vn_log, obj_dict):
        # Reference to policies
        if not 'network_policy_refs' in obj_dict:
            return
        pols = obj_dict['network_policy_refs']
        for pol_ref in pols:
            pol_name = ":".join(pol_ref['to'])
            maj_num = pol_ref['attr']['sequence']['major']
            min_num = pol_ref['attr']['sequence']['minor']
            vn_policy = VnPolicy(vnp_major=maj_num, vnp_minor=min_num,
                                 vnp_name=pol_name)
            vn_log.attached_policies.append(vn_policy)
    #end add_virtual_network_refs

    # uuid is parent's for collections
    def _http_get_common(self, request, uuid = None):
        # TODO check api + resource perms etc.
        if self._args.multi_tenancy and uuid:
            return self._permissions.check_perms_read(request, uuid)

        return (True, '')
    #end _http_get_common

    def _http_put_common(self, request, obj_type, obj_dict, obj_uuid):
        if obj_dict:
            fq_name_str = ":".join(obj_dict['fq_name'])

            # TODO keep _id_perms.uuid_xxlong immutable in future
            # dsetia - check with ajay regarding comment above
            #if 'id_perms' in obj_dict:
            #    del obj_dict['id_perms']
            if 'id_perms' in obj_dict and obj_dict['id_perms']['uuid']:
                if not self._db_conn.match_uuid(obj_dict, obj_uuid):
                    log_msg = 'UUID mismatch from %s:%s' \
                        %(request.environ['REMOTE_ADDR'], request.environ['HTTP_USER_AGENT'])
                    self.config_object_error(obj_uuid, fq_name_str, obj_type, 'put', log_msg)
                    self._db_conn.set_uuid(obj_dict, uuid.UUID(obj_uuid))

            apiConfig = VncApiCommon()
            apiConfig.operation = 'put'
            apiConfig.url = request.url
            apiConfig.identifier_uuid = obj_uuid
            # TODO should be from x-auth-token
            apiConfig.user = ''
            apiConfig.identifier_name = fq_name_str
            uveLog = None

            if obj_type == "virtual_machine" or obj_type == "virtual-machine":
                log = VMLog(api_log = apiConfig, sandesh=self._sandesh)
            elif obj_type == "virtual_network" or obj_type == "virtual-network":
                vn_log = UveVirtualNetworkConfig(name = fq_name_str,
                                                 attached_policies=[])
                self.add_virtual_network_refs(vn_log, obj_dict)
                uveLog = UveVirtualNetworkConfigTrace(data = vn_log,
                                                   sandesh=self._sandesh)
                log = VNLog(api_log = apiConfig, sandesh=self._sandesh)
            elif obj_type == "virtual_router" or obj_type == "virtual-router":
                log = VRLog(api_log = apiConfig, sandesh=self._sandesh)
            else:
                log = VncApiConfigLog(api_log = apiConfig,
                                      sandesh=self._sandesh)

            if uveLog:
                uveLog.send(sandesh=self._sandesh)
            log.send(sandesh=self._sandesh)

        # TODO check api + resource perms etc.
        if self._args.multi_tenancy:
            return self._permissions.check_perms_write(request, obj_uuid)

        return (True, '')
    #end _http_put_common

    # parent_type needed for perms check. None for derived objects (eg. routing-instance)
    def _http_delete_common(self, request, obj_type, uuid, parent_type):
        fq_name_str = ":".join(self._db_conn.uuid_to_fq_name(uuid))
        apiConfig = VncApiCommon(identifier_name=fq_name_str)
        apiConfig.operation = 'delete'
        apiConfig.url = request.url
        uuid_str = str(uuid)
        apiConfig.identifier_uuid = uuid_str
        apiConfig.identifier_name = fq_name_str
        uveLog = None

        if obj_type == "virtual_machine" or obj_type == "virtual-machine":
            log = VMLog(api_log = apiConfig, sandesh=self._sandesh)
        elif obj_type == "virtual_network" or obj_type == "virtual-network":
            vn_log = UveVirtualNetworkConfig(name = fq_name_str)
            vn_log.deleted = True
            uveLog = UveVirtualNetworkConfigTrace(data = vn_log,
                                               sandesh=self._sandesh)
            log = VNLog(api_log = apiConfig, sandesh=self._sandesh)
        elif obj_type == "virtual_router" or obj_type == "virtual-router":
            log = VRLog(api_log = apiConfig, sandesh=self._sandesh)
        else:
            log = VncApiConfigLog(api_log = apiConfig, sandesh=self._sandesh)

        if uveLog:
            uveLog.send(sandesh=self._sandesh)
        log.send(sandesh=self._sandesh)

        # TODO check api + resource perms etc.
        if not self._args.multi_tenancy or not parent_type:
            return (True, '')

        """
        Validate parent allows write access. Implicitly trust
        parent info in the object since coming from our DB.
        """
        obj_dict = self._db_conn.uuid_to_obj_dict(uuid)
        parent_fq_name = json.loads(obj_dict['fq_name'])[:-1]
        try:
            parent_uuid = self._db_conn.fq_name_to_uuid(parent_type, parent_fq_name)
        except NoIdError:
            # parent uuid could be null for derived resources such as routing-instance
            return (True, '')
        return self._permissions.check_perms_write(request, parent_uuid)
    #end _http_delete_common

    def _http_post_common(self, request, obj_type, obj_dict):
        if not obj_dict:
            # TODO check api + resource perms etc.
            return (True, None)

        # Fail if object exists already
        try:
            obj_uuid = self._db_conn.fq_name_to_uuid(obj_type, obj_dict['fq_name'])
            bottle.abort(409, '' + pformat(obj_dict['fq_name']) + ' already exists with uuid: ' + obj_uuid)
        except NoIdError:
            pass

        # Ensure object has atleast default permissions set
        self._ensure_id_perms_present(obj_type, obj_dict)

        # TODO check api + resource perms etc.

        uuid_in_req = obj_dict.get('uuid', None)

        fq_name_str = ":".join(obj_dict['fq_name'])
        apiConfig = VncApiCommon(identifier_name = fq_name_str)
        apiConfig.operation = 'post'
        apiConfig.url = request.url
        if uuid_in_req:
            apiConfig.identifier_uuid = uuid_in_req
        ## TODO should be from x-auth-token
        apiConfig.user = ''
        uveLog = None

        if obj_type == "virtual_machine" or obj_type == "virtual-machine":
            log = VMLog(api_log = apiConfig, sandesh=self._sandesh)
        elif obj_type == "virtual_network" or obj_type == "virtual-network":
            vn_log = UveVirtualNetworkConfig(name = fq_name_str,
                                             attached_policies=[])
            self.add_virtual_network_refs(vn_log, obj_dict)
            uveLog = UveVirtualNetworkConfigTrace(data = vn_log,
                                               sandesh=self._sandesh)
            log = VNLog(api_log = apiConfig, sandesh=self._sandesh)
        elif obj_type == "virtual_router" or obj_type == "virtual-router":
            log = VRLog(api_log = apiConfig, sandesh=self._sandesh)
        else:
            log = VncApiConfigLog(api_log = apiConfig, sandesh=self._sandesh)

        if uveLog:
            uveLog.send(sandesh=self._sandesh)
        log.send(sandesh=self._sandesh)

        return (True, uuid_in_req)
    #end _http_post_common

    def _init_logging(self):
        ifmap_logger = logging.getLogger('ifmap.client')

        fh = logging.FileHandler('ifmap.client.out')
        fh.setLevel(logging.ERROR)

        ch = logging.StreamHandler()
        ch.setLevel(logging.ERROR)

        ifmap_logger.addHandler(ch)
        ifmap_logger.addHandler(fh)

    #end _init_logging

    def cleanup(self):
        # TODO cleanup sandesh context
        pass
    #end cleanup

    # allocate block of IP addresses from VN. Subnet info expected in request body
    def vn_ip_alloc_http_post(self, id):
        try:
            vn_fq_name = self._db_conn.uuid_to_fq_name(id)
        except NoIdError:
            bottle.abort(404, 'Virtual Network ' + id + ' not found!')
        
        # expected format {"subnet" : "2.1.1.0/24", "count" : 4}
        req_dict = bottle.request.json
        count = req_dict['count'] if 'count' in req_dict else 1
        subnet = req_dict['subnet'] if 'subnet' in req_dict else None
        result = vnc_cfg_types.VirtualNetworkServer.ip_alloc(vn_fq_name, subnet, count)
        return result
    #end vn_ip_alloc_http_post

    # free block of ip addresses to subnet
    def vn_ip_free_http_post(self, id):
        try:
            vn_fq_name = self._db_conn.uuid_to_fq_name(id)
        except NoIdError:
            bottle.abort(404, 'Virtual Network ' + id + ' not found!')
    
        """ 
          {
            "subnet" : "2.1.1.0/24",
            "ip_addr": [ "2.1.1.239", "2.1.1.238", "2.1.1.237", "2.1.1.236" ]
          }
        """
    
        req_dict = bottle.request.json
        ip_list = req_dict['ip_addr'] if 'ip_addr' in req_dict else []
        subnet = req_dict['subnet'] if 'subnet' in req_dict else None
        result = vnc_cfg_types.VirtualNetworkServer.ip_free(vn_fq_name, subnet, ip_list)
        return result
    #end vn_ip_free_http_post

    # return no. of  IP addresses from VN/Subnet
    def vn_subnet_ip_count_http_post(self, id):
        try:
            vn_fq_name = self._db_conn.uuid_to_fq_name(id)
        except NoIdError:
            bottle.abort(404, 'Virtual Network ' + id + ' not found!')
        
        # expected format {"subnet" : ["2.1.1.0/24", "1.1.1.0/24"]
        req_dict = bottle.request.json
        (_, obj_dict) = self._db_conn.dbe_read('virtual-network', {'uuid': id})
        subnet_list = req_dict['subnet_list'] if 'subnet_list' in req_dict else []
        result = vnc_cfg_types.VirtualNetworkServer.subnet_ip_count(obj_dict, subnet_list)
        return result
    #end vn_subnet_ip_count_http_post

    def mt_http_get(self):
        pipe_start_app = self.get_pipe_start_app()
        mt = False
        try:
            mt = pipe_start_app.get_mt()
        except AttributeError:
            pass
        return {'enabled': mt}
    #end

    def mt_http_put(self):
        multi_tenancy = bottle.request.json['enabled']
        user_token = bottle.request.get_header('X-Auth-Token')
        if user_token is None:
            bottle.abort(403, " Permission denied")

        data = self._auth_svc.verify_signed_token(user_token)
        if data is None:
            bottle.abort(403, " Permission denied")

        pipe_start_app = self.get_pipe_start_app()
        try:
            pipe_start_app.set_mt(multi_tenancy)
        except AttributeError:
            pass
        self._args.multi_tenancy = multi_tenancy
        return {}
    #end

    def publish(self):
        # publish API server
        data = {
            'ip-address' : self._args.ifmap_server_ip,
            'port'       : self._args.listen_port,
        }
        self.api_server_task = self._disc.publish(ModuleNames[Module.API_SERVER], data)

        # publish ifmap server
        data = {
            'ip-address' : self._args.ifmap_server_ip,
            'port'       : self._args.ifmap_server_port,
        }
        self.ifmap_task = self._disc.publish(ModuleNames[Module.IFMAP_SERVER], data)
    #end

    def _create_default_security_group(self, proj_obj):
        sgr_uuid = str(uuid.uuid4())
        ingress_rule = PolicyRuleType(rule_uuid=sgr_uuid, direction='>',
            protocol='any',
            src_addresses=[AddressType(subnet=SubnetType('0.0.0.0', 0))],
            src_ports=[PortType(0, 65535)],
            dst_addresses=[AddressType(security_group='local')],
            dst_ports=[PortType(0, 65535)])
        sg_rules = PolicyEntriesType([ingress_rule])

        sgr_uuid = str(uuid.uuid4())
        egress_rule = PolicyRuleType(rule_uuid=sgr_uuid, direction='>',
            protocol='any',
            src_addresses=[AddressType(security_group='local')],
            src_ports=[PortType(0, 65535)],
            dst_addresses=[AddressType(subnet=SubnetType('0.0.0.0', 0))],
            dst_ports=[PortType(0, 65535)])
        sg_rules.add_policy_rule(egress_rule)

        #create security group
        sg_obj = SecurityGroup(name='default', parent_obj=proj_obj, 
            id_perms = self._get_default_id_perms('project'),
            security_group_entries=sg_rules)
        sg_obj_json = json.dumps(sg_obj, default=lambda o: dict((k, v) for k, v in o.__dict__.iteritems()))
        sg_obj_dict = json.loads(sg_obj_json)

        (ok, result) = self._db_conn.dbe_alloc('security_group', sg_obj_dict)
        obj_ids = result
        (ok, result) = self._db_conn.dbe_create('security_group', obj_ids, sg_obj_dict)
    #end _create_default_security_group

    def projects_http_post(self):
        resp = super(VncApiServer, self).projects_http_post()
        proj_obj = Project.factory(**resp['project'])
        self._create_default_security_group(proj_obj)
        return resp
    #end projects_http_post

    def project_http_delete(self, id):
        # delete the 'default' security group on project delete in openstack
        # TBD do this in openstack extension package
        obj_ids = {'uuid': id}
        db_conn = self._db_conn
        (read_ok, read_result) = db_conn.dbe_read('project', obj_ids)
        if read_ok:
            security_groups = read_result.get('security_groups', [])
            for sg in security_groups:
                sg_name = sg['to'][-1]
                sg_ids = {'uuid': sg['uuid']}
                if sg_name == 'default':
                    ifmap_id = cfgm_common.imid.get_ifmap_id_from_fq_name('security-group', sg['to'])
                    sg_ids['imid'] = ifmap_id
                    parent_imid = cfgm_common.imid.get_ifmap_id_from_fq_name('project', sg['to'][:-1])
                    sg_ids['parent_imid'] = parent_imid
                    db_conn.dbe_delete('security-group', sg_ids)

        super(VncApiServer, self).project_http_delete(id)
    #end project_http_delete

#end class VncApiServer

def main(args_str = None):
    vnc_api_server = VncApiServer(args_str)
    pipe_start_app = vnc_api_server.get_pipe_start_app()

    server_ip = vnc_api_server.get_listen_ip()
    server_port = vnc_api_server.get_server_port()

    # Advertise services
    if vnc_api_server._args.disc_server_ip and vnc_api_server._args.disc_server_port:
        vnc_api_server.publish()

    """ @sigchld
    Disable handling of SIG_CHLD for now as every keystone request to validate
    token sends SIG_CHLD signal to API server.
    """
    #hub.signal(signal.SIGCHLD, vnc_api_server.sigchld_handler)

    try:
        bottle.run(app = pipe_start_app, host = server_ip, port = server_port,
                   server = 'gevent')
    except Exception as e:
        # cleanup gracefully
        vnc_api_server.cleanup()

#end main

if __name__ == "__main__":
    import cgitb
    cgitb.enable(format='text')

    main()
