
"""
The serializer should be able to (de)serialize information that we want
to send over the wire from the client side to the agent side via
1- ZMQ
2- MongoDB

we except:
    1- Callables with and without dependecies.
    2- Non-callables like classes and other python objects
"""
__copyright__ = "Copyright 2013-2016, http://radical.rutgers.edu"
__license__   = "MIT"

import os
import dill
import pickle
import codecs
import tempfile


_obj_dir       = tempfile.gettempdir()
_obj_file_name = 'rp_obj.pkl'
_obj_file_path = os.path.join(_obj_dir, _obj_file_name)


def serialize_obj(obj):
    """
    serialize object
    """
    result    = None
    exception = None

    if callable(obj):
        try:
            result = dill.dumps(obj)
        except Exception as e:
            exception = e
            pass

        # if we fail, then pikle it by reference
        if result is None:
            # see issue: https://github.com/uqfoundation/dill/issues/128
            try:
                result = dill.dumps(obj, byref = True)
            except Exception as e:
                exception = e
                pass

    else:
        try:
            result = dill.dumps(obj, recurse = True)
        except Exception as e:
            exception = e
            pass

        if not result:
            # see issue: https://github.com/uqfoundation/dill/issues/128
            try:
                result = dill.dumps(obj, byref = True)
            except Exception as e:
                exception = e
                pass

    if result is None:
        raise Exception("object %s is not serializable") from exception

    return  result


def serialize_file(obj):
    """
    serialize object to file
    # FIXME: assign unique path and id for the pickled file
    #        to avoid overwriting to the same file
    """
    result    = None
    exception = None

    if callable(obj):
        try:
            with open(_obj_file_path, 'wb') as f:
                dill.dump(obj, f)
                result = _obj_file_path

        except Exception as e:
            exception = e
            pass

    else:
        try:
            with open(_obj_file_path, 'wb') as f:
                dill.dump(obj, f, recurse = True)
                result = _obj_file_path
        except Exception as e:
            exception = e
            pass

    if result is None:
        raise Exception("object is not serializable") from exception

    return _obj_file_path



def deserialize_file(fname):
    """
    Deserialize object from file
    """
    result = None

    # check if we have a valid file
    if not os.path.isfile(fname):
        return None

    try:
        with open(fname, 'rb') as f:
            result = dill.load(f)
            if result is None:
                raise RuntimeError('failed to deserialize')
            return result

    except Exception as e:
        raise Exception ("failed to deserialize object from file") from e


def deserialize_obj(obj):
    """
    Deserialize object from str
    """
    result = None

    try:
        result = dill.loads(obj)
        if not result:
            raise RuntimeError('failed to deserialize')
        return result

    except Exception as e:
        raise Exception ("failed to deserialize from object") from e


def serialize_bson(obj):

    result = codecs.encode(pickle.dumps(obj), "base64").decode()

    return result


def deserialize_bson(obj):

    result = pickle.loads(codecs.decode(obj.encode(), "base64"))

    return result
