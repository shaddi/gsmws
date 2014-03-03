from fabric.api import cd, lcd, local, env, run, settings
from fabric.operations import run, sudo

env.host = "localhost"

def bts1():
    env.command_socket = "/var/run/command"
    env.name = "openbts1"
    env.openbts_apps = "/home/openbts/src/openbts-p4-ucb/rP4.0.0RC3/openbts/apps"

def bts2():
    env.command_socket = "/var/run/command2"
    env.name = "openbts2"
    env.openbts_apps = "/home/openbts/src/openbts-p4-ucb/rP4.0.0RC3/openbts/apps"

def cli():
    with lcd("/home/openbts/src/openbts-p4-ucb/rP4.0.0RC3/openbts/apps"):
        local("sudo ./OpenBTSCLI %s" % env.command_socket)

def stop():
    local("sudo supervisorctl stop %s" % env.name)

def start():
    local("sudo supervisorctl start %s" % env.name)

def restart():
    local("sudo supervisorctl restart %s" % env.name)

def demo():
    local("sudo supervisorctl start openbts1")
    local("sudo supervisorctl start openbts2")
    local("sudo supervisorctl start gsmws")

def finish():
    local("sudo supervisorctl stop openbts1")
    local("sudo supervisorctl stop openbts2")
    local("sudo supervisorctl stop gsmws")

