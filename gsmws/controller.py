import time
import datetime
import random
import sqlite3
import logging
import sys
import threading
from os.path import expanduser

import envoy

import decoder
import gsm

class BTS(object):
    def __init__(self, db_loc, openbts_proc, trans_proc, gsm_decoder, loglvl=logging.DEBUG):
        self.process_name = openbts_proc
        self.transceiver_process = trans_proc

        self.cmd_socket = None # TODO
        self.neighbor_table = None # TODO
        self.decoder = gsmd
        self.loglvl = loglvl

        self.decoder.start() # start the decoder for this BTS

    @property
    def current_arfcn(self):
        return self.decoder.current_arfcn

    def config(self, config_str):
        """ Run a config command as though we're using the OpenBTSCLI """

        # THIS IS THE OFFICIAL WAY TO DO THIS
        # IN THE NAME OF ALL THAT IS HOLY
        r = envoy.run("echo -n '%s' | sudo /OpenBTS/OpenBTSDo" % config_str)
        return r.std_out


    def restart(self):
        """ TODO OpenBTS should really be a service, and we should really just say
        "sudo service restart openbts". But we can't, because OpenBTS is a disaster.
        EVEN WORSE, we assume that we're in OpenBTS's runloop which will restart us
        automatically. What a mess... """
        logging.warning("Restarting %s..." % self.process_name)
        envoy.run("killall %s %s" % (self.process_name, self.transceiver_process))
        time.sleep(10)
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

        # now, update the neighbor table for each
        # TODO

"""
The controller has three tasks:
    0) Pick channels to monitor and configure OpenBTS accordingly
    1) Regularly check the measurement reports from the decoder to decide what channels are in use
    2) If we detect a channel in use "near" us, we should stop OpenBTS and pick a new channel (TODO)
"""
class Controller(object):
    def __init__(self, bts, nct, sleep, gsmwsdb, loglvl=logging.DEBUG):
        self.bts = bts
        self.NEIGHBOR_CYCLE_TIME = nct # seconds to wait before switching up the neighbor list
        self.SLEEP_TIME = sleep # seconds between rssi checks

        self.gsmwsdb_location = gsmwsdb
        self.gsmwsdb_lock = threading.Lock()
        self.gsmwsdb = sqlite3.connect(gsmwsdb)

        self.loglvl = loglvl
        logging.basicConfig(format='%(asctime)s %(module)s %(funcName)s %(lineno)d %(levelname)s %(message)s', filename='/var/log/gsmws.log',level=loglvl)
        logging.warning("New controller started.")

    def initdb(self):
        with self.gsmwsdb_lock:
            self.gsmwsdb.execute("CREATE TABLE IF NOT EXISTS AVAIL_ARFCN (TIMESTAMP TEXT NOT NULL, ARFCN INTEGER, RSSI REAL);")
            self.gsmwsdb.execute("CREATE TABLE IF NOT EXISTS MAX_STRENGTHS (TIMESTAMP TEXT NOT NULL, ARFCN INTEGER, RSSI REAL);")
            self.gsmwsdb.execute("CREATE TABLE IF NOT EXISTS AVG_STRENGTHS (TIMESTAMP TEXT NOT NULL, ARFCN INTEGER, RSSI REAL, COUNT INTEGER);")

    def restart_openbts(self):
        """ TODO OpenBTS should really be a service, and we should really just say
        "sudo service restart openbts". But we can't, because OpenBTS is a disaster.
        EVEN WORSE, we assume that we're in OpenBTS's runloop which will restart us
        automatically. What a mess... """
        logging.warning("Restarting OpenBTS...")
        envoy.run("killall %s %s" % (OPENBTS_PROCESS_NAME, TRANSCEIVER_PROCESS_NAME))
        time.sleep(10)
        if len(envoy.run("ps aux | grep './OpenBTS'"))==0:
            pass # TODO: run OpenBTS

    def set_new_neighbor_list(self, neighbors):
        neighbor_string = " ".join([str(_) for _  in neighbors])

        # THIS IS THE OFFICIAL WAY TO DO THIS
        # IN THE NAME OF ALL THAT IS HOLY
        envoy.run("echo -n 'config GSM.CellSelection.Neighbors %s' | sudo /OpenBTS/OpenBTSDo" % neighbor_string)

        # ignore measurement requests for a while
        logging.info("New neighbor list: %s" % neighbor_string)

    def change_arfcn(self, new_arfcn, immediate=False):
        """ Change OpenBTS to use a new ARFCN. By default, just update the DB, but
        don't actually restart OpenBTS. If immediate=True, restart OpenBTS too. """
        self.openbtsdb.execute("UPDATE CONFIG SET VALUESTRING=? WHERE KEYSTRING='GSM.Radio.C0'", (new_arfcn,))
        self.openbtsdb.commit()
        try:
            assert int(new_arfcn) <= 124
            assert int(new_arfcn) > 0
        except:
            logging.error("Invalid ARFCN: %s" % new_arfcn)
            return
        logging.warning("Updated next ARFCN to %s" % new_arfcn)
        if immediate:
            self.restart_openbts()

    def update_rssi_db(self, rssis):
        # rssis: A dict of ARFCN->RSSI that's up to date as of now (it already captures our historical knowledge)
        with self.gsmwsdb_lock:
            logging.info("Updating RSSIs: %s" % rssis)
            existing = [arfcn for res in self.gsmwsdb.execute("SELECT ARFCN FROM AVAIL_ARFCN").fetchall() for arfcn in res]
            timestamp = datetime.datetime.now()

            for arfcn in existing:
                if arfcn in rssis:
                    # do update
                    self.gsmwsdb.execute("UPDATE AVAIL_ARFCN SET TIMESTAMP=?, RSSI=? WHERE ARFCN=?", (timestamp, rssis[arfcn], arfcn))
            for arfcn in [_ for _ in rssis if _ not in existing]:
                # do insert
                self.gsmwsdb.execute("INSERT INTO AVAIL_ARFCN VALUES(?,?,?)", (timestamp, arfcn, rssis[arfcn]))
            self.gsmwsdb.commit()

            # now, expire!
            now = datetime.datetime.now()
            res = self.gsmwsdb.execute("SELECT TIMESTAMP, ARFCN FROM AVAIL_ARFCN")
            for items in res.fetchall():
                ts = datetime.datetime.strptime(items[0], "%Y-%m-%d %H:%M:%S.%f")
                arfcn = items[1]
                if (now - ts).seconds > 4*self.NEIGHBOR_CYCLE_TIME:
                    self.gsmwsdb.execute("DELETE FROM AVAIL_ARFCN WHERE ARFCN=?", (arfcn,))
                    logging.debug("Expiring ARFCN %s (%s)" % (arfcn, ts))
            self.gsmwsdb.commit()

    def safe_arfcns(self):
        """ Get the ARFCNs which probably have no other users """
        with self.gsmwsdb_lock:
            res = self.gsmwsdb.execute("SELECT ARFCN, RSSI FROM AVAIL_ARFCN")
        candidates = []
        for i in res:
            arfcn, rssi = i
            if rssi < 0:
                candidates.append(arfcn)
        return candidates

    def pick_new_safe_arfcn(self):
        """ Returns a random ARFCN that we have verified to be safe (i.e., <0 RSSI) """
        return random.choice(self.safe_arfcns())

    def pick_new_neighbors(self):
        """ Pick a set of ARFCNs we haven't scanned before """
        with self.gsmwsdb_lock:
            existing = [arfcn for res in self.gsmwsdb.execute("SELECT ARFCN FROM AVAIL_ARFCN").fetchall() for arfcn in res]
        return random.sample([_ for _ in range(1,124) if _ not in existing], 5)

    def main(self, stream=None, cmd=None):
        self.initdb() # set up the gsmws db

        if stream==None:
            if cmd==None:
                cmd = "tshark -V -n -i any udp dst port 4729"
            stream = gsm.command_stream(cmd)
        self.gsmd = decoder.GSMDecoder(stream, self.gsmwsdb_lock, self.gsmwsdb_location, loglvl=self.loglvl)
        self.gsmd.start()
        last_cycle_time = datetime.datetime.now()
        ignored_since = datetime.datetime.now()
        while True:
            try:
                now = datetime.datetime.now()
                if self.gsmd.ignore_reports and (now - ignored_since).seconds > 120:
                    self.gsmd.ignore_reports = False

                td = (now - last_cycle_time)
                if td.seconds > self.NEIGHBOR_CYCLE_TIME:
                    try:
                        new_arfcn = self.pick_new_safe_arfcn()
                        self.change_arfcn(new_arfcn)
                    except IndexError:
                        logging.error("Unable to pick new safe ARFCN!")
                        pass # just don't pick for now
                    self.set_new_neighbor_list(self.pick_new_neighbors())
                    self.gsmd.ignore_reports = True
                    ignored_since = now
                    last_cycle_time = now

                logging.info("Current ARFCN: %s" % self.gsmd.current_arfcn)

                rssis = self.gsmd.rssi()

                # TODO this might actually be the right behavior -- why does
                # the fact we used an arfcn before change whether we need to
                # get a consistent clear scan before using it again? As long as
                # it becomes a candidate again later this is fine.
                #del(rssis[self.gsmd.current_arfcn]) # ignore readings for our own C0 (else, we never consider our own used arfcn safe until we scan it 100 times again!)
                self.update_rssi_db(rssis)
                logging.info("Safe ARFCNs: %s" % str(self.safe_arfcns()))
                time.sleep(self.SLEEP_TIME)
            except KeyboardInterrupt:
                break
