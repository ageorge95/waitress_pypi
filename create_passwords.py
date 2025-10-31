import os
from passlib.apache import HtpasswdFile

htpasswd_file = '.htpasswd'
if not os.path.exists(htpasswd_file):
    ht = HtpasswdFile(htpasswd_file, new=True)
    ht.set_password('anon', 'password')
    ht.save()