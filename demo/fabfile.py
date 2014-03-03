from fabric.api import cd, lcd, local, env, run, settings
from fabric.operations import run, sudo

def bts1():
    env.command_socket = "/var/run/command"
    env.name = "openbts1"
    env.openbts_apps = "/home/openbts/src/openbts-p4-ucb/rP4.0.0RC3/openbts/apps"

def bts2():
    env.command_socket = "/var/run/command2"
    env.name = "openbts2"
    env.openbts_apps = "/home/openbts/src/openbts-p4-ucb/rP4.0.0RC3/openbts/apps"

def cli():
    with cd("/home/openbts/src/openbts-p4-ucb/rP4.0.0RC3/openbts/apps"):
        sudo("./OpenBTSCLI %s" % env.command_socket)

def stop():
    sudo("supervisorctl stop %s" % env.name)

def start():
    sudo("supervisorctl start %s" % env.name)

def restart():
    sudo("supervisorctl restart %s" % env.name)

