import time
import datetime
import random
import sqlite3
import logging
import threading

import decoder
import gsm
import bts

"""
The controller has three tasks:
    0) Pick channels to monitor and configure OpenBTS accordingly
    1) Regularly check the measurement reports from the decoder to decide what channels are in use
    2) If we detect a channel in use "near" us, we should stop OpenBTS and pick a new channel (TODO)
"""
class Controller(object):
    def __init__(self, db_loc, openbts_proc, trans_proc, nct, sleep, gsmwsdb, loglvl=logging.DEBUG, bts_class=bts.BTS):
        self.OPENBTS_PROCESS_NAME=openbts_proc
        self.TRANSCEIVER_PROCESS_NAME=trans_proc
        self.NEIGHBOR_CYCLE_TIME = nct # seconds to wait before switching up the neighbor list
        self.SLEEP_TIME = sleep # seconds between rssi checks

        self.openbtsdb_loc = db_loc

        self.gsmwsdb_location = gsmwsdb
        self.gsmwsdb_lock = threading.Lock()
        self.gsmwsdb = sqlite3.connect(gsmwsdb)

        self.bts = None
        self.bts_class = bts_class

        self.loglvl = loglvl
        logging.basicConfig(format='%(asctime)s %(module)s %(funcName)s %(lineno)d %(levelname)s %(message)s', filename='/var/log/gsmws.log',level=loglvl)
        logging.warning("New controller started.")

    def initdb(self):
        with self.gsmwsdb_lock:
            self.gsmwsdb.execute("CREATE TABLE IF NOT EXISTS AVAIL_ARFCN (TIMESTAMP TEXT NOT NULL, ARFCN INTEGER, RSSI REAL);")
            self.gsmwsdb.execute("CREATE TABLE IF NOT EXISTS MAX_STRENGTHS (TIMESTAMP TEXT NOT NULL, ARFCN INTEGER, RSSI REAL);")
            self.gsmwsdb.execute("CREATE TABLE IF NOT EXISTS AVG_STRENGTHS (TIMESTAMP TEXT NOT NULL, ARFCN INTEGER, RSSI REAL, COUNT INTEGER);")

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

        gsmd = decoder.GSMDecoder(stream, self.gsmwsdb_lock, self.gsmwsdb_location, loglvl=self.loglvl)
        self.bts = self.bts_class(self.openbtsdb_loc, self.OPENBTS_PROCESS_NAME, self.TRANSCEIVER_PROCESS_NAME, self.loglvl)
        self.bts.init_decoder(gsmd)
        last_cycle_time = datetime.datetime.now()
        ignored_since = datetime.datetime.now()
        while True:
            try:
                now = datetime.datetime.now()

                if self.bts.decoder.ignore_reports and (now - ignored_since).seconds > 120:
                    self.bts.decoder.ignore_reports = False

                td = (now - last_cycle_time)
                if td.seconds > self.NEIGHBOR_CYCLE_TIME:
                    try:
                        new_arfcn = self.pick_new_safe_arfcn()
                        self.change_arfcn(new_arfcn)
                    except IndexError:
                        logging.error("Unable to pick new safe ARFCN!")
                        pass # just don't pick for now
                    self.bts.set_neighbors(self.pick_new_neighbors())
                    self.bts.decoder.ignore_reports = True
                    ignored_since = now
                    last_cycle_time = now

                logging.info("Current ARFCN: %s" % self.bts.current_arfcn)

                rssis = self.bts.decoder.rssi()

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
