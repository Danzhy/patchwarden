import hashlib
import json


def token_for(user):
    return hashlib.md5(user.encode()).hexdigest()
