import datetime
import time
import sqlite3
import logging

import envoy


# these are the potential txatten states that each BTS can be in.
# algorithm is as follows:
# T0: on
# T0 + T - ST*3: l3
# T0 + T - ST*2: l2
# T0 + T - ST: l1
# T0 + T: off

class BTS(object):
    def __init__(self, db_loc, openbts_proc, trans_proc, loglvl=logging.DEBUG, id_num=0, start_time=None, cycle_time=90):
        self.process_name = openbts_proc
        self.transceiver_process = trans_proc

        openbtsdb = sqlite3.connect(db_loc)
        self.cmd_socket = openbtsdb.execute("SELECT VALUESTRING FROM CONFIG WHERE KEYSTRING=?", ("CLI.SocketPath",)).fetchall()[0][0]
        openbtsdb.close() # don't touch this anymore

        # HACK XXX XXX FIXME
        if id_num == 0:
            self.neighbor_table = sqlite3.connect("/var/run/NeighborTable.db")
        else:
            self.neighbor_table = sqlite3.connect("/var/run/NeighborTable%d.db" % (id_num + 1))
        #self.neighbor_table = sqlite3.connect(self.config("config Peering.NeighborTable.Path").split()[1]) # likewise, from the openbts.db
        self.neighbors = []
        self.neighbor_offset = 0

        self.loglvl = loglvl

        self.decoder = None # we can't create our own, since we need a global gsmwsdb_lock from controller

        self.id_num = id_num

        # state management
        self.state = None
        self.last_switch = None
        self.txattens = {0: 1, 1: 20, 2: 40, 3: 80}
        self.cycle_time = cycle_time
        if not start_time:
            self.start_time = datetime.datetime.now()
        else:
            self.start_time = start_time

    def timefloor(self, dt, fl=10):
        return datetime.datetime(dt.year, dt.month, dt.day, dt.hour, dt.second - (dt.second % fl))

    def next_atten_state(self):
        """
        Picks one of four possible state levels. This ensures that one BTS is
        always in state 0 (full power). This is safe to call all the time!
        """
        now = datetime.datetime.now()
        n = self.timefloor(now)
        t = int((n - self.start_time).total_seconds())
        sec = (t % (self.cycle_time * 2) - (self.cycle_time - 10)) / 10.
        state = min(3, max(0, int(sec)))
        if state != self.state:
            self.state = state
            self.last_switch = now
            logging.info("txatten BTS %s is now %d dBm (state %s)", (self.id_num, self.txattens[state], self.state))
            self.config("txatten %d" % self.txattens[state])

    def init_decoder(self, gsm_decoder):
        """ Start the decoder for this BTS. We do this separately since we want
        to be able to create the BTS external to the controller, but we don't
        have a global DB lock until the controller starts."""
        self.decoder = gsm_decoder
        self.decoder.start()

    def is_off(self):
        """
        We define the BTS as off if it's in txatten state 3 and has been there
        for at least 10 seconds.
        """
        s_since_switch = int((self.last_switch - datetime.datetime.now()).total_seconds())
        off = self.state == 3 and s_since_switch > 10
        return off

    @property
    def current_arfcn(self):
        return self.decoder.current_arfcn

    @property
    def last_arfcns(self):
        return self.decoder.last_arfcns

    @property
    def reports(self):
        return self.decoder.reports.getall()


    def config(self, config_str):
        """ Run a config command as though we're using the OpenBTSCLI """

        # THIS IS THE OFFICIAL WAY TO DO THIS
        # IN THE NAME OF ALL THAT IS HOLY
        # TODO: expanduser here?
        r = envoy.run("echo '%s' | sudo /home/openbts/OpenBTSDo %s" % (config_str, self.cmd_socket))
        return r.std_out.strip()


    def restart(self):
        """ TODO OpenBTS should really be a service, and we should really just say
        "sudo service restart openbts". But we can't, because OpenBTS is a disaster.
        EVEN WORSE, we assume that we're in OpenBTS's runloop which will restart us
        automatically. What a mess... """
        logging.warning("Restarting %s..." % self.process_name)

        # HACK XXX
        # get the pid of our transceiver and kill it, thus restarting openbts
        r = envoy.run("ps aux").std_out.split("\n")
        target = "transceiver 1 %d" % self.id_num
        pid = None
        for item in r:
            if target in item:
                pid = item.split()[1]
                break

        envoy.run("kill %s" % pid)
        time.sleep(1)

        #envoy.run("killall %s %s" % (self.process_name, self.transceiver_process))
        #time.sleep(2)
        #if len(envoy.run("ps aux | grep './%s'" % self.process_name).std_out)==0:
        #    pass # TODO: run OpenBTS

    def change_arfcn(self, new_arfcn, immediate=False):
        """ Change OpenBTS to use a new ARFCN. By default, just update the DB, but
        don't actually restart OpenBTS. If immediate=True, restart OpenBTS too. """
        self.config("config GSM.Radio.C0 %s" % new_arfcn)
        try:
            assert int(new_arfcn) <= 124
            assert int(new_arfcn) > 0
        except:
            logging.error("Invalid ARFCN: %s" % new_arfcn)
            return
        logging.warning("Updated next ARFCN to %s" % new_arfcn)
        if immediate:
            self.restart()

    def set_neighbors(self, arfcns, port=16001, num_real=None):
        """
        The new OpenBTS handover feature makes setting the neighbor list a bit
        more complicated. You're supposed to just set the IP addresses of the
        neighbor cells, and then OpenBTS populates a neighbor table DB
        (/var/run/NeighborTable.db by default, set in Peering.NeighborTable.Path)
        with ARFCN, BSIC, etc by directly querying the other BTS. Manually
        populating this DB requires two steps. First, we have to add IP addresses
        to GSM.Neighbors; if we don't, anything we add to the neighbor table will
        be deleted. Once we've added an IP, we can manually update the
        NeighborTable with our list of neighbor ARFCNs.

        Our algorithm here is to use unrouteable 127.0.10.0/24 addresses for our
        neighbors; we simply incrementally add neighbor IPs based on how many we
        have. Once we've done that, we can directly manipulate the neighbor table
        using those IP addresses.

        If we have a real BTS, we just assume the first arfcn is the ARFCN for
        that BTS, and ignore it.
        """
        if num_real != None:
            real_ip_str = "127.0.0.1"
            self.neighbor_offset = (self.neighbor_offset + 1) % 2
            fake_ips  = ["127.0.9.%d" % (num+1+self.neighbor_offset) for num in range(1,len(arfcns))]
            fake_ip_str = " ".join([str(_) for _  in fake_ips])
        else:
            real_ip_str = ""
            self.neighbor_offset = (self.neighbor_offset + 1) % 2
            fake_ips  = ["127.0.9.%d" % (num+1+self.neighbor_offset) for num in range(0,len(arfcns))]
            fake_ip_str = " ".join([str(_) for _  in fake_ips])

        self.neighbors = arfcns

        # set IPs in openbts
        conf_string = "config GSM.Neighbors %s %s" % (real_ip_str, fake_ip_str)
        r = self.config(conf_string)
        logging.debug("Updating neighbors (%s) with conf string '%s': '%s'" % (arfcns, conf_string, r))

        # TODO: bug here: if the database locks, we're fucked. Should handle this but can't remember exact exception (on plane)
        # now, update the neighbor table for each
        # IP: one of our fake IPs
        # Updated: Set to ten seconds ago
        # Holdoff: GSM.Handover.FailureHoldoff, time in seconds between holdoff attempts. Set to a gazillion here.
        # C0: The ARFCN we want to scan
        # BSIC: The BSIC. Can be set to whatever.
        updated = int(datetime.datetime.now().strftime("%s")) - 10 # ten seconds ago, unix time
        holdoff = 2**20 # 12 days... YOLO! (and only check once, heh heh)
        bsic = 1 # TODO does this matter?
        for i in range(0, len(fake_ips)):
            ip = "%s:%d" % (fake_ips[i], port)
            if num_real:
                arfcn = arfcns[i+1]
            else:
                arfcn = arfcns[i]
            self.neighbor_table.execute("DELETE FROM NEIGHBOR_TABLE WHERE C0=?", (arfcn,))
            self.neighbor_table.execute("DELETE FROM NEIGHBOR_TABLE WHERE IPADDRESS=?", (ip,))
            self.neighbor_table.execute("INSERT INTO NEIGHBOR_TABLE VALUES (?, ?, ?, ?, ?)", (ip, updated, holdoff, arfcn, bsic))
        self.neighbor_table.commit()

class OldBTS(BTS):
    def __init__(self, db_loc, openbts_proc, trans_proc, loglvl=logging.DEBUG):
        self.process_name = openbts_proc
        self.transceiver_process = trans_proc

        openbtsdb = sqlite3.connect(db_loc)
        self.cmd_socket = openbtsdb.execute("SELECT VALUESTRING FROM CONFIG WHERE KEYSTRING=?", ("CLI.SocketPath",)).fetchall()[0][0]
        openbtsdb.close() # don't touch this anymore

        self.loglvl = loglvl

        self.decoder = None # we can't create our own, since we need a global gsmwsdb_lock from controller

    def set_neighbors(self, arfcns):
        neighbor_string = " ".join([str(_) for _  in arfcns])

        self.config("config GSM.CellSelection.Neighbors %s" % neighbor_string)

        # ignore measurement requests for a while
        logging.info("New neighbor list: %s" % neighbor_string)
