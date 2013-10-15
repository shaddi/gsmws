import time
import datetime
import random
import sqlite3
import logging
import sys
from os.path import expanduser

import envoy

import decoder
import gsm

"""
The controller has three tasks:
    0) Pick channels to monitor and configure OpenBTS accordingly
    1) Regularly check the measurement reports from the decoder to decide what channels are in use
    2) If we detect a channel in use "near" us, we should stop OpenBTS and pick a new channel (TODO)
"""
class Controller(object):
    def __init__(self, openbts_db_loc, openbts_proc, trans_proc, nct, sleep, gsmwsdb, loglvl=logging.DEBUG):
        self.OPENBTS_PROCESS_NAME=openbts_proc
        self.TRANSCEIVER_PROCESS_NAME=trans_proc
        self.NEIGHBOR_CYCLE_TIME = nct # seconds to wait before switching up the neighbor list
        self.SLEEP_TIME = sleep # seconds between rssi checks

        self.gsmwsdb = sqlite3.connect(gsmwsdb)
        self.openbtsdb = sqlite3.connect(openbts_db_loc)

        self.loglvl = loglvl
        logging.basicConfig(format='%(asctime)s %(module)s %(funcName)s %(lineno)d %(levelname)s %(message)s', filename='/var/log/gsmws.log',level=loglvl)

    def initdb(self):
        self.gsmwsdb.execute("CREATE TABLE IF NOT EXISTS AVAIL_ARFCN (TIMESTAMP TEXT NOT NULL, ARFCN INTEGER, RSSI REAL);")

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
        self.openbtsdb.execute("UPDATE CONFIG SET VALUESTRING=? WHERE KEYSTRING='GSM.CellSelection.Neighbors'", (neighbor_string,))
        self.openbtsdb.commit()
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

    def safe_arfcns(self):
        """ Get the ARFCNs which probably have no other users """
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
        existing = [arfcn for res in self.gsmwsdb.execute("SELECT ARFCN FROM AVAIL_ARFCN").fetchall() for arfcn in res]
        return random.sample([_ for _ in range(1,124) if _ not in existing], 5)

    def main(self, stream=None, cmd=None):
        self.initdb() # set up the gsmws db

        if stream==None:
            if cmd==None:
                cmd = "tshark -V -n -i any udp dst port 4729"
            stream = gsm.command_stream(cmd)
        self.gsmd = decoder.GSMDecoder(stream, loglvl=self.loglvl)
        self.gsmd.start()
        last_cycle_time = datetime.datetime.now()
        while True:
            try:
                td = (datetime.datetime.now() - last_cycle_time)
                print td.seconds
                if td.seconds > self.NEIGHBOR_CYCLE_TIME:
                    try:
                        new_arfcn = self.pick_new_safe_arfcn()
                        self.change_arfcn(new_arfcn)
                    except IndexError:
                        logging.error("Unable to pick new safe ARFCN!")
                        pass # just don't pick for now
                    self.set_new_neighbor_list(self.pick_new_neighbors())
                    last_cycle_time = datetime.datetime.now()

                logging.info("Current ARFCN: %s" % self.gsmd.current_arfcn)

                rssis = self.gsmd.rssi()
                self.update_rssi_db(rssis)
                logging.info("Safe ARFCNs: %s" % str(self.safe_arfcns()))
                time.sleep(self.SLEEP_TIME)
            except KeyboardInterrupt:
                break

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="GSMWS Controller.")
    parser.add_argument('--openbtsdb', type=str, action='store', default='/etc/OpenBTS/OpenBTS.db', help="OpenBTS.db location")
    parser.add_argument('--openbts', type=str, action='store', default='OpenBTS', help="OpenBTS process name")
    parser.add_argument('--transceiver', type=str, action='store', default='transceiver', help="transceiver process name")
    parser.add_argument('--cycle', '-c', type=int, action='store', default=14400, help="Time before switching to new set of neighbors to scan (seconds).")
    parser.add_argument('--sleep', '-s', type=int, action='store', default=10, help="Time to sleep between RSSI checks (seconds)")
    parser.add_argument('--gsmwsdb', type=str, action='store', default=expanduser("~") + "/gsmws.db", help="Where to store the gsmws.db file")
    parser.add_argument('--cmd', type=str, action='store', default=None, help="Command string to run.")
    parser.add_argument('--stdin', action='store_true', help="Read from STDIN")
    parser.add_argument('--debug', action='store_true', help="Enable debug logging")
    args = parser.parse_args()

    #OPENBTS_DB_LOC="/etc/OpenBTS/OpenBTS.db"
    #OPENBTS_PROCESS_NAME="OpenBTS"
    #TRANSCEIVER_PROCESS_NAME="transceiver"
    #NEIGHBOR_CYCLE_TIME = 4*60*60 # seconds to wait before switching up the neighbor list
    #SLEEP_TIME = 10 # seconds between rssi checks
    #GSMWS_DB = expanduser("~") + "/gsmws.db"

    OPENBTS_DB_LOC=args.openbtsdb
    OPENBTS_PROCESS_NAME=args.openbts
    TRANSCEIVER_PROCESS_NAME=args.transceiver
    NEIGHBOR_CYCLE_TIME = args.cycle # seconds to wait before switching up the neighbor list
    SLEEP_TIME = args.sleep # seconds between rssi checks
    GSMWS_DB = args.gsmwsdb

    if args.debug:
        loglvl = logging.DEBUG
    else:
        loglvl = logging.INFO

    c = Controller(OPENBTS_DB_LOC, OPENBTS_PROCESS_NAME, TRANSCEIVER_PROCESS_NAME, NEIGHBOR_CYCLE_TIME, SLEEP_TIME, GSMWS_DB, loglvl=loglvl)
    if args.stdin:
        c.main(stream=sys.stdin)
    else:
        c.main(cmd=args.cmd)
