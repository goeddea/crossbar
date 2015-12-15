#####################################################################################
#
#  Copyright (C) Tavendo GmbH
#
#  Unless a separate license agreement exists between you and Tavendo GmbH (e.g. you
#  have purchased a commercial license), the license terms below apply.
#
#  Should you enter into a separate license agreement after having received a copy of
#  this software, then the terms of such license agreement replace the terms below at
#  the time at which such license agreement becomes effective.
#
#  In case a separate license agreement ends, and such agreement ends without being
#  replaced by another separate license agreement, the license terms below apply
#  from the time at which said agreement ends.
#
#  LICENSE TERMS
#
#  This program is free software: you can redistribute it and/or modify it under the
#  terms of the GNU Affero General Public License, version 3, as published by the
#  Free Software Foundation. This program is distributed in the hope that it will be
#  useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
#  See the GNU Affero General Public License Version 3 for more details.
#
#  You should have received a copy of the GNU Affero General Public license along
#  with this program. If not, see <http://www.gnu.org/licenses/agpl-3.0.en.html>.
#
#####################################################################################

from __future__ import absolute_import

import os
import re
import json
import traceback
import socket

import twisted
from twisted.internet.defer import inlineCallbacks, Deferred

from autobahn.wamp.types import CallDetails, CallOptions, ComponentConfig
from autobahn.wamp.exception import ApplicationError
from autobahn.twisted.wamp import ApplicationRunner

from crossbar.router.router import RouterFactory
from crossbar.router.session import RouterSessionFactory
from crossbar.router.service import RouterServiceSession
from crossbar.worker.router import RouterRealm
from crossbar.common import checkconfig
from crossbar.controller.process import NodeControllerSession
from crossbar.controller.management import NodeManagementBridgeSession
from crossbar.controller.management import NodeManagementSession

from crossbar._logging import make_logger

__all__ = ('Node',)


class Node(object):
    """
    A Crossbar.io node is the running a controller process and one or multiple
    worker processes.

    A single Crossbar.io node runs exactly one instance of this class, hence
    this class can be considered a system singleton.
    """

    log = make_logger()

    def __init__(self, cbdir=None, reactor=None):
        """

        :param cbdir: The node directory to run from.
        :type cbdir: unicode
        :param reactor: Reactor to run on.
        :type reactor: obj or None
        """
        # node directory
        self._cbdir = cbdir or u'.'

        # reactor we should run on
        if reactor is None:
            from twisted.internet import reactor
        self._reactor = reactor

        # the node's name (must be unique within the management realm)
        self._node_id = None

        # the node's management realm
        self._realm = None

        # config of this node.
        self._config = None

        # node controller session (a singleton ApplicationSession embedded
        # in the local node router)
        self._controller = None

        # when run in "managed mode", this will hold the uplink WAMP session
        # from the node controller to the mananagement application
        self._manager = None

        # node shutdown triggers, one or more of checkconfig.NODE_SHUTDOWN_MODES
        self._node_shutdown_triggers = [checkconfig.NODE_SHUTDOWN_ON_WORKER_EXIT]

    def load(self, configfile):
        """
        Check and load the node configuration (usually, from ".crossbar/config.json").
        """
        configpath = os.path.join(self._cbdir, configfile)

        self.log.debug("Loading node configuration from '{configpath}' ..",
                       configpath=configpath)

        self._config = checkconfig.check_config_file(configpath, silence=True)

        self.log.info("Node configuration loaded from '{configfile}'",
                      configfile=configfile)

    @inlineCallbacks
    def start(self):
        """
        Starts this node. This will start a node controller and then spawn new worker
        processes as needed.
        """
        if not self._config:
            raise Exception("No node configuration loaded")

        controller_config = self._config.get('controller', {})
        controller_options = controller_config.get('options', {})

        # set controller process title
        #
        try:
            import setproctitle
        except ImportError:
            self.log.warn("Warning, could not set process title (setproctitle not installed)")
        else:
            setproctitle.setproctitle(controller_options.get('title', 'crossbar-controller'))

        # the node's local realm
        #
        self._realm = controller_config.get('realm', 'crossbar')

        # the node's name (must be unique within the management realm when running
        # in "managed mode")
        #
        if 'id' in controller_config:
            self._node_id = controller_config['id']
        else:
            self._node_id = socket.gethostname()

        # standalone vs managed mode
        #
        if 'cdc' in controller_config:

            cdc_config = controller_config['cdc']

            # connecting transport configuration for uplink to management app
            if 'transport' in cdc_config:
                transport = cdc_config['transport']
            else:
                transport = {
                    "type": "websocket",
                    "url": "wss://devops.crossbario.com/ws",
                    "endpoint": {
                        "type": "tcp",
                        "host": "devops.crossbario.com",
                        "port": 443,
                        "timeout": 5,
                        "tls": {
                            "hostname": "devops.crossbario.com"
                        }
                    }
                }

            # the node's cdc (management) realm
            realm = cdc_config['realm']

            extra = {
                'node': self,
                'onready': Deferred(),
                'onexit': Deferred(),

                # authentication information for connecting to uplink CDC router
                # using WAMP-CRA authentication
                # WAMP
                'authid': self._node_id,
                'authkey': cdc_config['key']
            }

            runner = ApplicationRunner(url=transport['url'], realm=realm, extra=extra,
                                       debug=False, debug_wamp=False)

            try:
                self.log.info("CDC connecting to {url} ..", url=transport['url'])
                yield runner.run(NodeManagementSession, start_reactor=False)

                # wait until we have attached to the uplink CDC
                self._manager = yield extra['onready']
            except Exception as e:
                raise Exception("Could not connect to CDC - {}".format(e))

            # in managed mode, a node - by default - only shuts down when explicitly asked to,
            # or upon a fatal error in the node controller
            self._node_shutdown_triggers = [checkconfig.NODE_SHUTDOWN_ON_SHUTDOWN_REQUESTED]

            self.log.info("Connected to Crossbar.io DevOps Center (CDC)")
        else:
            self._manager = None

            # in standalone mode, a node - by default - is immediately shutting down whenever
            # a worker exits (successfully or with error)
            self._node_shutdown_triggers = [checkconfig.NODE_SHUTDOWN_ON_WORKER_EXIT]

        # allow to override node shutdown triggers
        #
        if 'shutdown' in controller_options:
            self.log.info("Overriding default node shutdown triggers with {} from node config".format(controller_options['shutdown']))
            self._node_shutdown_triggers = controller_options['shutdown']
        else:
            self.log.info("Using default node shutdown triggers {}".format(self._node_shutdown_triggers))

        # router and factory that creates router sessions
        #
        self._router_factory = RouterFactory()
        self._router_session_factory = RouterSessionFactory(self._router_factory)

        rlm_config = {
            'name': self._realm
        }
        rlm = RouterRealm(None, rlm_config)

        # create a new router for the realm
        router = self._router_factory.start_realm(rlm)

        # add a router/realm service session
        cfg = ComponentConfig(self._realm)

        rlm.session = RouterServiceSession(cfg, router)
        self._router_session_factory.add(rlm.session, authrole=u'trusted')

        if self._manager:
            self._bridge_session = NodeManagementBridgeSession(cfg, self, self._manager)
            self._router_session_factory.add(self._bridge_session, authrole=u'trusted')
        else:
            self._bridge_session = None

        # the node controller singleton WAMP application session
        #
        self._controller = NodeControllerSession(self)

        # add the node controller singleton session to the router
        #
        self._router_session_factory.add(self._controller, authrole=u'trusted')

        # Detect WAMPlets
        #
        wamplets = self._controller._get_wamplets()
        if len(wamplets) > 0:
            self.log.info("Detected {wamplets} WAMPlets in environment:",
                          wamplets=len(wamplets))
            for wpl in wamplets:
                self.log.info("WAMPlet {dist}.{name}",
                              dist=wpl['dist'], name=wpl['name'])
        else:
            self.log.debug("No WAMPlets detected in enviroment.")

        panic = False

        try:
            yield self._startup(self._config)
        except ApplicationError as e:
            panic = True
            self.log.error("{msg}", msg=e.error_message())
        except Exception:
            panic = True
            traceback.print_exc()

        if panic:
            try:
                self._reactor.stop()
            except twisted.internet.error.ReactorNotRunning:
                pass

    @inlineCallbacks
    def _startup(self, config):
        # fake call details information when calling into
        # remoted procedure locally
        #
        call_details = CallDetails(caller=0)

        controller = config.get('controller', {})

        # start Manhole in node controller
        #
        if 'manhole' in controller:
            yield self._controller.start_manhole(controller['manhole'], details=call_details)

        # startup all workers
        #
        worker_no = 1

        call_options = CallOptions(disclose_me=True)

        for worker in config.get('workers', []):
            # worker ID, type and logname
            #
            if 'id' in worker:
                worker_id = worker.pop('id')
            else:
                worker_id = 'worker{}'.format(worker_no)
                worker_no += 1

            worker_type = worker['type']
            worker_options = worker.get('options', {})

            if worker_type == 'router':
                worker_logname = "Router '{}'".format(worker_id)

            elif worker_type == 'container':
                worker_logname = "Container '{}'".format(worker_id)

            elif worker_type == 'websocket-testee':
                worker_logname = "WebSocketTestee '{}'".format(worker_id)

            elif worker_type == 'guest':
                worker_logname = "Guest '{}'".format(worker_id)

            else:
                raise Exception("logic error")

            # router/container
            #
            if worker_type in ['router', 'container', 'websocket-testee']:

                # start a new native worker process ..
                #
                if worker_type == 'router':
                    yield self._controller.start_router(worker_id, worker_options, details=call_details)

                elif worker_type == 'container':
                    yield self._controller.start_container(worker_id, worker_options, details=call_details)

                elif worker_type == 'websocket-testee':
                    yield self._controller.start_websocket_testee(worker_id, worker_options, details=call_details)

                else:
                    raise Exception("logic error")

                # setup native worker generic stuff
                #
                if 'pythonpath' in worker_options:
                    added_paths = yield self._controller.call('crossbar.node.{}.worker.{}.add_pythonpath'.format(self._node_id, worker_id), worker_options['pythonpath'], options=call_options)
                    self.log.debug("{worker}: PYTHONPATH extended for {paths}",
                                   worker=worker_logname, paths=added_paths)

                if 'cpu_affinity' in worker_options:
                    new_affinity = yield self._controller.call('crossbar.node.{}.worker.{}.set_cpu_affinity'.format(self._node_id, worker_id), worker_options['cpu_affinity'], options=call_options)
                    self.log.debug("{worker}: CPU affinity set to {affinity}",
                                   worker=worker_logname, affinity=new_affinity)

                if 'manhole' in worker:
                    yield self._controller.call('crossbar.node.{}.worker.{}.start_manhole'.format(self._node_id, worker_id), worker['manhole'], options=call_options)
                    self.log.debug("{worker}: manhole started",
                                   worker=worker_logname)

                # setup router worker
                #
                if worker_type == 'router':

                    # start realms on router
                    #
                    realm_no = 1

                    for realm in worker.get('realms', []):

                        if 'id' in realm:
                            realm_id = realm.pop('id')
                        else:
                            realm_id = 'realm{}'.format(realm_no)
                            realm_no += 1

                        # extract schema information from WAMP-flavored Markdown
                        #
                        schemas = None
                        if 'schemas' in realm:
                            schemas = {}
                            schema_pat = re.compile(r"```javascript(.*?)```", re.DOTALL)
                            cnt_files = 0
                            cnt_decls = 0
                            for schema_file in realm.pop('schemas'):
                                schema_file = os.path.join(self._cbdir, schema_file)
                                self.log.info("{worker}: processing WAMP-flavored Markdown file {schema_file} for WAMP schema declarations",
                                              worker=worker_logname, schema_file=schema_file)
                                with open(schema_file, 'r') as f:
                                    cnt_files += 1
                                    for d in schema_pat.findall(f.read()):
                                        try:
                                            o = json.loads(d)
                                            if isinstance(o, dict) and '$schema' in o and o['$schema'] == u'http://wamp.ws/schema#':
                                                uri = o['uri']
                                                if uri not in schemas:
                                                    schemas[uri] = {}
                                                schemas[uri].update(o)
                                                cnt_decls += 1
                                        except Exception:
                                            self.log.failure("{worker}: WARNING - failed to process declaration in {schema_file} - {log_failure.value}",
                                                             worker=worker_logname, schema_file=schema_file)
                            self.log.info("{worker}: processed {cnt_files} files extracting {cnt_decls} schema declarations and {len_schemas} URIs",
                                          worker=worker_logname, cnt_files=cnt_files, cnt_decls=cnt_decls, len_schemas=len(schemas))

                        yield self._controller.call('crossbar.node.{}.worker.{}.start_router_realm'.format(self._node_id, worker_id), realm_id, realm, schemas, options=call_options)
                        self.log.info("{worker}: realm '{realm_id}' (named '{realm_name}') started",
                                      worker=worker_logname, realm_id=realm_id, realm_name=realm['name'])

                        # add roles to realm
                        #
                        role_no = 1
                        for role in realm.get('roles', []):
                            if 'id' in role:
                                role_id = role.pop('id')
                            else:
                                role_id = 'role{}'.format(role_no)
                                role_no += 1

                            yield self._controller.call('crossbar.node.{}.worker.{}.start_router_realm_role'.format(self._node_id, worker_id), realm_id, role_id, role, options=call_options)
                            self.log.info("{}: role '{}' (named '{}') started on realm '{}'".format(worker_logname, role_id, role['name'], realm_id))

                        # start uplinks for realm
                        #
                        uplink_no = 1
                        for uplink in realm.get('uplinks', []):
                            if 'id' in uplink:
                                uplink_id = uplink.pop('id')
                            else:
                                uplink_id = 'uplink{}'.format(uplink_no)
                                uplink_no += 1

                            yield self._controller.call('crossbar.node.{}.worker.{}.start_router_realm_uplink'.format(self._node_id, worker_id), realm_id, uplink_id, uplink, options=call_options)
                            self.log.info("{}: uplink '{}' started on realm '{}'".format(worker_logname, uplink_id, realm_id))

                    # start connections (such as PostgreSQL database connection pools)
                    # to run embedded in the router
                    #
                    connection_no = 1

                    for connection in worker.get('connections', []):

                        if 'id' in connection:
                            connection_id = connection.pop('id')
                        else:
                            connection_id = 'connection{}'.format(connection_no)
                            connection_no += 1

                        yield self._controller.call('crossbar.node.{}.worker.{}.start_connection'.format(self._node_id, worker_id), connection_id, connection, options=call_options)
                        self.log.info("{}: connection '{}' started".format(worker_logname, connection_id))

                    # start components to run embedded in the router
                    #
                    component_no = 1

                    for component in worker.get('components', []):

                        if 'id' in component:
                            component_id = component.pop('id')
                        else:
                            component_id = 'component{}'.format(component_no)
                            component_no += 1

                        yield self._controller.call('crossbar.node.{}.worker.{}.start_router_component'.format(self._node_id, worker_id), component_id, component, options=call_options)
                        self.log.info("{}: component '{}' started".format(worker_logname, component_id))

                    # start transports on router
                    #
                    transport_no = 1

                    for transport in worker['transports']:

                        if 'id' in transport:
                            transport_id = transport.pop('id')
                        else:
                            transport_id = 'transport{}'.format(transport_no)
                            transport_no += 1

                        yield self._controller.call('crossbar.node.{}.worker.{}.start_router_transport'.format(self._node_id, worker_id), transport_id, transport, options=call_options)
                        self.log.info("{}: transport '{}' started".format(worker_logname, transport_id))

                # setup container worker
                #
                elif worker_type == 'container':

                    component_no = 1

                    # if components exit "very soon after" we try to
                    # start them, we consider that a failure and shut
                    # our node down. We remove this subscription 2
                    # seconds after we're done starting everything
                    # (see below). This is necessary as
                    # start_container_component returns as soon as
                    # we've established a connection to the component
                    def component_exited(info):
                        component_id = info.get("id")
                        self.log.critical("Component '{component_id}' failed to start; shutting down node.", component_id=component_id)
                        try:
                            self._reactor.stop()
                        except twisted.internet.error.ReactorNotRunning:
                            pass
                    topic = 'crossbar.node.{}.worker.{}.container.on_component_stop'.format(self._node_id, worker_id)
                    component_stop_sub = yield self._controller.subscribe(component_exited, topic)

                    # start connections (such as PostgreSQL database connection pools)
                    # to run embedded in the container
                    #
                    connection_no = 1

                    for connection in worker.get('connections', []):

                        if 'id' in connection:
                            connection_id = connection.pop('id')
                        else:
                            connection_id = 'connection{}'.format(connection_no)
                            connection_no += 1

                        yield self._controller.call('crossbar.node.{}.worker.{}.start_connection'.format(self._node_id, worker_id), connection_id, connection, options=call_options)
                        self.log.info("{}: connection '{}' started".format(worker_logname, connection_id))

                    # start components to run embedded in the container
                    #
                    for component in worker.get('components', []):

                        if 'id' in component:
                            component_id = component.pop('id')
                        else:
                            component_id = 'component{}'.format(component_no)
                            component_no += 1

                        yield self._controller.call('crossbar.node.{}.worker.{}.start_container_component'.format(self._node_id, worker_id), component_id, component, options=call_options)
                        self.log.info("{worker}: component '{component_id}' started",
                                      worker=worker_logname, component_id=component_id)

                    # after 2 seconds, consider all the application components running
                    self._reactor.callLater(2, component_stop_sub.unsubscribe)

                # setup websocket-testee worker
                #
                elif worker_type == 'websocket-testee':

                    # start transports on router
                    #
                    transport = worker['transport']
                    transport_no = 1
                    transport_id = 'transport{}'.format(transport_no)

                    yield self._controller.call('crossbar.node.{}.worker.{}.start_websocket_testee_transport'.format(self._node_id, worker_id), transport_id, transport, options=call_options)
                    self.log.info("{}: transport '{}' started".format(worker_logname, transport_id))

                else:
                    raise Exception("logic error")

            elif worker_type == 'guest':

                # start guest worker
                #
                yield self._controller.start_guest(worker_id, worker, details=call_details)
                self.log.info("{worker}: started", worker=worker_logname)

            else:
                raise Exception("logic error")
