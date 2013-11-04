'''
This module contains the classes necessary to handle both
`JSONRPC <http://json-rpc.org/>`_ and
`XMLRPC <http://www.xmlrpc.com/>`_ requests.
It also contains a decorator to mark methods as rpc methods.
'''

import inspect
import pydoc
import types
from django.contrib.auth import authenticate, login, logout
from .jsonrpcdispatcher import JSONRPCDispatcher, json
from .xmlrpcdispatcher import XMLRPCDispatcher

try:
    # Python2.x
    from xmlrpclib import Fault, loads, ServerProxy
except ImportError:
    # Python3
    from xmlrpc.client import Fault, loads, ServerProxy

# this error code is taken from xmlrpc-epi
# http://xmlrpc-epi.sourceforge.net/specs/rfc.fault_codes.php
APPLICATION_ERROR = -32500


def rpcmethod(**kwargs):
    '''
    Accepts keyword based arguments that describe the method's rpc aspects

    **Parameters**

    ``name``
      the name of the method to make available via RPC.
      Defaults to the method's actual name
    ``signature``
      the signature of the method that will be returned by
      calls to the XMLRPC introspection method ``system.methodSignature``.
      It is of the form: [return_value, arg1, arg2, arg3, ...].
      All of the types should be XMLRPC types
      (eg. struct, int, array, etc. - see the XMLRPC spec for details).
    ``permission``
      the Django permission required to execute this method
    ``login_required``
      the method requires a user to be logged in

    **Examples**

    ::

        @rpcmethod()
        @rpcmethod(name='myns.myFuncName', signature=['int','int'])
        @rpcmethod(permission='add_group')
        @rpcmethod(login_required=True)

    '''

    def set_rpcmethod_info(method):
        method.is_rpcmethod = True
        method.signature = []
        method.permission = None
        method.login_required = False
        method.external_name = getattr(method, '__name__')

        if 'name' in kwargs:
            method.external_name = kwargs['name']

        if 'signature' in kwargs:
            method.signature = kwargs['signature']

        if 'permission' in kwargs:
            method.permission = kwargs['permission']

        if 'login_required' in kwargs:
            method.login_required = kwargs['login_required']

        return method
    return set_rpcmethod_info


class RPCMethod(object):
    '''
    A method available to be called via the rpc dispatcher

    **Attributes**

    ``method``
      The underlying Python method to call when this method is invoked
    ``help``
      Help message (usually the docstring) printed by the introspection
      functions when detail about a method is requested
    ``name``
      name of the method by which it can be called
    ``signature``
      See :meth:`rpc4django.rpcdispatcher.rpcmethod`
    ``permission``
      Any Django permissions required to call this method
    ``login_required``
      The method can only be called by a logged in user

    '''

    def __init__(self, method, name=None, signature=None, docstring=None):

        self.method = method
        self.help = ''
        self.signature = []
        self.name = ''
        self.permission = None
        self.login_required = False
        self.args = []

        # set the method name based on @rpcmethod or the passed value
        # default to the actual method name
        if hasattr(method, 'external_name'):
            self.name = method.external_name
        elif name is not None:
            self.name = name
        else:
            try:
                # Python2
                self.name = method.func_name
            except AttributeError:
                # Python3
                self.name = method.__name__

        # get the help string for each method
        if docstring is not None:
            self.help = docstring
        else:
            self.help = pydoc.getdoc(method)

        # set the permissions based on the decorator
        self.permission = getattr(method, 'permission', None)

        # set the permissions based on the decorator
        self.login_required = getattr(method, 'login_required', self.permission is not None)

        # use inspection (reflection) to get the arguments
        args, varargs, keywords, defaults = inspect.getargspec(method)
        self.args = [arg for arg in args if arg != 'self']
        self.signature = ['object' for arg in self.args]
        self.signature.insert(0, 'object')

        if hasattr(method, 'signature') and \
           len(method.signature) == len(self.args) + 1:
            # use the @rpcmethod signature if it has the correct
            # number of args
            self.signature = method.signature
        elif signature is not None and len(self.args) + 1 == len(signature):
            # use the passed signature if it has the correct number
            # of arguments
            self.signature = signature

    def get_stub(self):
        '''
        Returns JSON for a JSONRPC request for this method

        This is used to generate the introspection method output
        '''

        params = self.get_params()
        plist = ['"' + param['name'] + '"' for param in params]

        jsonlist = [
            '{',
            '"id": "djangorpc",',
            '"method": "' + self.name + '",',
            '"params": [',
            '   ' + ','.join(plist),
            ']',
            '}',
        ]

        return '\n'.join(jsonlist)

    def get_returnvalue(self):
        '''
        Returns the return value which is the first element of the signature
        '''
        if len(self.signature) > 0:
            return self.signature[0]
        return None

    def get_params(self):
        '''
        Returns a list of dictionaries containing name and type of the params

        eg. [{'name': 'arg1', 'rpctype': 'int'},
             {'name': 'arg2', 'rpctype': 'int'}]
        '''
        if len(self.signature) > 0:
            arglist = []
            if len(self.signature) == len(self.args) + 1:
                for argnum in range(len(self.args)):
                    arglist.append({'name': self.args[argnum],
                                    'rpctype': self.signature[argnum + 1]})
                return arglist
            else:
                # this should not happen under normal usage
                for argnum in range(len(self.args)):
                    arglist.append({'name': self.args[argnum],
                                    'rpctype': 'object'})
                return arglist
        return []


class RPCDispatcher(object):
    '''
    Keeps track of the methods available to be called and then
    dispatches method calls to either the
    :class:`XMLRPCDispatcher <rpc4django.xmlrpcdispatcher.XMLRPCDispatcher>`
    or the
    :class:`JSONRPCDispatcher <rpc4django.jsonrpcdispatcher.JSONRPCDispatcher>`

    Disables RPC introspection methods (eg. ``system.list_methods()`` if
    ``restrict_introspection`` is set to ``True``. Disables out of the box
    authentication if ``restrict_ootb_auth`` is ``True``.

    **Attributes**

    ``url``
      The URL that handles RPC requests (eg. ``/RPC2``)
      This is needed by ``system.describe``.
    ``rpcmethods``
      A list of :class:`RPCMethod<rpc4django.rpcdispatcher.RPCMethod>` instances
      available to be called by the dispatcher
    ``xmlrpcdispatcher``
      An instance of :class:`XMLRPCDispatcher <rpc4django.xmlrpcdispatcher.XMLRPCDispatcher>`
      where XMLRPC calls are dispatched to using :meth:`xmldispatch`
    ``jsonrpcdispatcher``
      An instance of :class:`JSONRPCDispatcher <rpc4django.jsonrpcdispatcher.JSONRPCDispatcher>`
      where JSONRPC calls are dispatched to using :meth:`jsondispatch`

    '''

    def __init__(self, url='', apps=[], restrict_introspection=False,
                 restrict_ootb_auth=True, json_encoder=None):
        self.url = url
        self.rpcmethods = []        # a list of RPCMethod objects
        self.jsonrpcdispatcher = JSONRPCDispatcher(json_encoder)
        self.xmlrpcdispatcher = XMLRPCDispatcher()

        if not restrict_introspection:
            self.register_method(self.system_listmethods)
            self.register_method(self.system_methodhelp)
            self.register_method(self.system_methodsignature)
            self.register_method(self.system_describe)

        if not restrict_ootb_auth:
            self.register_method(self.system_login)
            self.register_method(self.system_logout)

        self.register_rpcmethods(apps)

    @rpcmethod(name='system.describe', signature=['struct'])
    def system_describe(self):
        '''
        Returns a simple method description of the methods supported
        '''

        description = {}
        description['serviceType'] = 'RPC4Django JSONRPC+XMLRPC'
        description['serviceURL'] = self.url,
        description['methods'] = [{'name': method.name,
                                   'summary': method.help,
                                   'params': method.get_params(),
                                   'return': method.get_returnvalue()}
                                  for method in self.rpcmethods]

        return description

    @rpcmethod(name='system.listMethods', signature=['array'])
    def system_listmethods(self):
        '''
        Returns a list of supported methods
        '''

        methods = [method.name for method in self.rpcmethods]
        methods.sort()
        return methods

    @rpcmethod(name='system.methodHelp', signature=['string', 'string'])
    def system_methodhelp(self, method_name):
        '''
        Returns documentation for a specified method
        '''

        for method in self.rpcmethods:
            if method.name == method_name:
                return method.help

        # this differs from what implementation in SimpleXMLRPCServer does
        # this will report via a fault or error while SimpleXMLRPCServer
        # just returns an empty string
        raise Fault(APPLICATION_ERROR, 'No method found with name: ' +
                    str(method_name))

    @rpcmethod(name='system.methodSignature', signature=['array', 'string'])
    def system_methodsignature(self, method_name):
        '''
        Returns the signature for a specified method
        '''

        for method in self.rpcmethods:
            if method.name == method_name:
                return method.signature
        raise Fault(APPLICATION_ERROR, 'No method found with name: ' +
                    str(method_name))

    @rpcmethod(name='system.login', signature=['boolean', 'string', 'string'])
    def system_login(self, username, password, **kwargs):
        '''
        Authorizes a user to enable sending protected RPC requests
        '''

        request = kwargs.get('request', None)
        user = authenticate(username=username, password=password)

        if user is not None and request is not None:
            if user.is_active:
                login(request, user)
                return True

        return False

    @rpcmethod(name='system.logout', signature=['boolean'])
    def system_logout(self, **kwargs):
        '''
        Deauthorizes a user
        '''

        request = kwargs.get('request', None)

        if request is not None:
            logout(request)
            return True

        return False

    def register_rpcmethods(self, apps):
        '''
        Scans the installed apps for methods with the rpcmethod decorator
        Adds these methods to the list of methods callable via RPC
        '''

        for appname in apps:
            # check each app for any rpcmethods
            try:
                app = __import__(appname, globals(), locals(), ['*'])
            except (ImportError, ValueError, TypeError):
                # import throws ValueError on empty "name"
                continue

            for obj in dir(app):
                method = getattr(app, obj)
                if isinstance(method, ServerProxy):
                    continue
                if callable(method) and \
                   hasattr(method, 'is_rpcmethod') and \
                   method.is_rpcmethod is True:
                    # if this method is callable and it has the rpcmethod
                    # decorator, add it to the dispatcher
                    self.register_method(method, method.external_name)
                elif isinstance(method, types.ModuleType):
                    # if this is not a method and instead a sub-module,
                    # scan the module for methods with @rpcmethod
                    self.register_rpcmethods(["%s.%s" % (appname, obj)])

    def jsondispatch(self, body, **kwargs):
        '''
        Sends the post data to :meth:`rpc4django.jsonrpcdispatcher.JSONRPCDispatcher.dispatch`
        '''

        return self.jsonrpcdispatcher.dispatch(body.decode('utf-8'), **kwargs)

    def xmldispatch(self, body, **kwargs):
        '''
        Sends the post data to :meth:`rpc4django.xmlrpcdispatcher.XMLRPCDispatcher.dispatch`
        '''

        return self.xmlrpcdispatcher.dispatch(body, **kwargs)

    def get_method_name(self, body, request_format='xml'):
        '''
        Gets the name of the method to be called given the post data
        and the format of the data
        '''

        if request_format == 'xml':
            # xmlrpclib.loads could throw an exception, but this is fine
            # since _marshaled_dispatch would throw the same thing
            try:
                params, method = loads(body.decode('utf-8'))
                return method
            except Exception:
                return None
        else:
            try:
                # attempt to do a json decode on the data
                jsondict = json.loads(body.decode('utf-8'))
                if not isinstance(jsondict, dict) or 'method' not in jsondict:
                    return None
                else:
                    return jsondict['method']
            except ValueError:
                return None

    def list_methods(self):
        '''
        Returns a list of RPCMethod objects supported by the server
        '''

        return self.rpcmethods

    def register_method(self, method, name=None, signature=None, helpmsg=None):
        '''
        Instantiates an RPCMethod object and adds it to ``rpcmethods``
        so that it can be called by RPC requests

        **Parameters**

        ``method``
          A callable Python method that the dispatcher will delegate to when
          requested via RPC
        ``name``
          The name to make the method availabe. ``None`` signifies to use
          the method's actual name
        ``signature``
          The signature of the method. See :meth:`rpc4django.rpcdispatcher.rpcmethod`
        ``helpmsg``
          The "help" message displayed by introspection functions asking about
          the method

        '''

        meth = RPCMethod(method, name, signature, helpmsg)

        if meth.name not in self.system_listmethods():
            self.xmlrpcdispatcher.register_function(method, meth.name)
            self.jsonrpcdispatcher.register_function(method, meth.name)
            self.rpcmethods.append(meth)
