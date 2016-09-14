#  -*- coding: utf-8 -*-
# *********************************************************************
# plankton - a library for creating hardware device simulators
# Copyright (C) 2016 European Spallation Source ERIC
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
# *********************************************************************

import zmq
import uuid
import types

try:
    import exceptions
except ImportError:
    import builtins as exceptions


class ServerSideException(Exception):
    def __init__(self, type, message):
        """
        This exception type replaces exceptions that are raised on the server,
        but unknown (i.e. not in the exceptions-module) on the client side.
        To retain as much information as possible, the exception type on the server and
        the message are stored.

        :param type: Type of the exception on the server side.
        :param message: Exception message on the server side.
        """
        super(ServerSideException, self).__init__(
            'Exception on server side of type \'{}\': \'{}\''.format(type, message))

        self.server_side_type = type
        self.server_side_message = message


class ProtocolException(Exception):
    """
    An exception type for exceptions related to the transport protocol, i.e.
    malformed requests etc.
    """


class ControlClient(object):
    def __init__(self, host='127.0.0.1', port='10000'):
        """
        This class provides an interface to a ControlServer instance on
        the server side. Proxies to exposed objects can be obtained either
        directly via get_object or, in case the server exposes a collection
        of objects at the top level, a dictionary of named objects can be
        obtained via get_object_collection.

        :param host: Host the control server is running on
        :param port: Port on which the control server is listening
        """
        self._socket = self._get_zmq_req_socket()
        self._socket.connect('tcp://{0}:{1}'.format(host, port))

    def _get_zmq_req_socket(self):
        context = zmq.Context()
        return context.socket(zmq.REQ)

    def json_rpc(self, method, *args):
        """
        This method takes a ZMQ REQ-socket and submits a JSON-object containing
        the RPC (JSON-RPC 2.0 format) to the supplied method with the supplied arguments.
        Then it waits for a reply from the server and blocks until it has received
        a JSON-response. The method returns the response and the id it used to tag
        the original request, which is a random UUID (uuid.uuid4).

        :param method: Method to call on remote.
        :param args: Arguments to method call.
        :return: JSON result and request id.
        """
        id = str(uuid.uuid4())
        self._socket.send_json(
            {'method': method,
             'params': args,
             'jsonrpc': '2.0',
             'id': id
             })

        return self._socket.recv_json(), id

    def get_object(self, object_name=''):
        api, request_id = self.json_rpc(object_name + ':api')

        if not 'result' in api or api['id'] != request_id:
            raise ProtocolException('Failed to retrieve API of remote object.')

        object_type = type(str(api['result']['class']), (ObjectProxy,), {})
        methods = api['result']['methods']

        glue = '.' if object_name else ''
        return object_type(self, methods, object_name + glue)

    def get_object_collection(self, object_name=''):
        """
        If the remote end exposes a collection of objects under the supplied object name (empty
        for top level), this method returns a dictionary of these objects stored under their
        names on the server.

        This function performs n + 1 calls to the server, where n is the number of objects.

        :param object_name: Object name on the server. This is required if the object collection is not the top level object.
        """

        object_names = self.get_object(object_name).get_objects()

        return {obj: self.get_object(obj) for obj in object_names}


class ObjectProxy(object):
    def __init__(self, connection, members, prefix=''):
        """
        This class serves as a base class for dynamically created classes on the
        client side that represent server-side objects. Upon initialization,
        this class takes the supplied methods and installs appropriate proxy methods
        or properties into the object and class respectively. Because of that
        class manipulation, this class must never be used directly.
        Instead, it should be used as a base-class for dynamically created types
        that mirror types on the server, like this:

            proxy = type('SomeClassName', (ObjectProxy, ), {})(connection, methods, prefix)

        There is however, the class ControlClient, which automates all that
        and provides objects that are ready to use.

        Exceptions on the server are propagated to the client. If the exception is not part
        of the exceptions-module (builtins for Python 3), a ServerSideException is raised instead
        which contains information about the server side exception.

        All RPC method names are prefixed with the supplied prefix, which is usually the
        object name on the server plus a dot.

        :param connection: ControlClient-object for remote calls.
        :param members: List of strings to generate methods and properties.
        :param prefix: Usually object name on the server plus dot.
        """
        self._properties = set()

        self._connection = connection
        self._prefix = prefix
        self._add_member_proxies(members)

    def _make_request(self, method, *args):
        """
        This method performs a JSON-RPC request via the object's ZMQ socket. If successful,
        the result is returned, otherwise exceptions are raised. Server side exceptions are
        raised using the same type as on the server if they are part of the exceptions-module.
        Otherwise, a ServerSideException is raised.

        :param method: Method of the object to call on the remote.
        :param args: Positional arguments to the method call.
        :return: Result of the remote call if successful.
        """
        response, id = self._connection.json_rpc(self._prefix + method, *args)

        if 'result' in response:
            return response['result']

        if 'error' in response:
            if 'data' in response['error']:
                exception_type = response['error']['data']['type']
                exception_message = response['error']['data']['message']

                if not hasattr(exceptions, exception_type):
                    raise ServerSideException(exception_type, exception_message)
                else:
                    exception = getattr(exceptions, exception_type)
                    raise exception(exception_message)
            else:
                raise ProtocolException(response['error']['message'])

    def _add_member_proxies(self, members):
        for member in [str(m) for m in members]:
            if ':set' in member or ':get' in member:
                self._properties.add(member.split(':')[-2].split('.')[-1])
            else:
                setattr(self, member, self._create_method_proxy(member))

        for prop in self._properties:
            setattr(type(self), prop, property(self._create_getter_proxy(prop),
                                               self._create_setter_proxy(prop)))

    def _create_getter_proxy(self, property_name):
        def getter(obj):
            return obj._make_request(property_name + ':get')

        return getter

    def _create_setter_proxy(self, property_name):
        def setter(obj, value):
            return obj._make_request(property_name + ':set', value)

        return setter

    def _create_method_proxy(self, method_name):
        def method_wrapper(obj, *args):
            return obj._make_request(method_name, *args)

        return types.MethodType(method_wrapper, self)
