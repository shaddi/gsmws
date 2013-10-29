import datetime
import time
import sqlite3
import logging

import envoy


class BTS(object):
    def __init__(self, db_loc, openbts_proc, trans_proc, loglvl=logging.DEBUG):
        self.process_name = openbts_proc
        self.transceiver_process = trans_proc

        openbtsdb = sqlite3.connect(db_loc)
        self.cmd_socket = openbtsdb.execute("SELECT VALUESTRING FROM CONFIG WHERE KEYSTRING=?", ("CLI.SocketPath",)).fetchall()[0][0]
        openbtsdb.close() # don't touch this anymore

        self.neighbor_table = sqlite3.connect(self.config("config Peering.NeighborTable.Path").split()[1]) # likewise, from the openbts.db

        self.loglvl = loglvl

        self.decoder = None # we can't create our own, since we need a global gsmwsdb_lock from controller


    def init_decoder(self, gsm_decoder):
        """ Start the decoder for this BTS. We do this separately since we want
        to be able to create the BTS external to the controller, but we don't
        have a global DB lock until the controller starts."""
        self.decoder = gsm_decoder
        self.decoder.start()

    @property
    def current_arfcn(self):
        return self.decoder.current_arfcn

    @property
    def reports(self):
        return self.decoder.reports.getall()


    def config(self, config_str):
        """ Run a config command as though we're using the OpenBTSCLI """

        # THIS IS THE OFFICIAL WAY TO DO THIS
        # IN THE NAME OF ALL THAT IS HOLY
        # TODO: expanduser here?
        r = envoy.run("echo -n '%s' | sudo /home/openbts/OpenBTSDo %s" % (config_str, self.cmd_socket))
        return r.std_out.strip()


    def restart(self):
        """ TODO OpenBTS should really be a service, and we should really just say
        "sudo service restart openbts". But we can't, because OpenBTS is a disaster.
        EVEN WORSE, we assume that we're in OpenBTS's runloop which will restart us
        automatically. What a mess... """
        logging.warning("Restarting %s..." % self.process_name)
        envoy.run("killall %s %s" % (self.process_name, self.transceiver_process))
        time.sleep(2)
        if len(envoy.run("ps aux | grep './%s'" % self.process_name))==0:
            pass # TODO: run OpenBTS

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

    def set_neighbors(self, arfcns):
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
        """
        fake_ips  = ["127.0.10.%d" % (num+1) for num in range(0,len(arfcns))]
        neighbors = dict(zip(arfcns, fake_ips))
        fake_ip_str = " ".join([str(_) for _  in fake_ips])

        # set IPs in openbts
        self.config("config GSM.Neighbors %s" % fake_ip_str)

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
        for i in range(0, len(arfcns)):
            ip = fake_ips[i]
            arfcn = arfcns[i]
            self.neighbor_table.execute("DELETE FROM NEIGHBOR_TABLE WHERE C0=?", (arfcn,))
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
