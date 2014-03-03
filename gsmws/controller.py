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
            logging.debug("Updating RSSIs: %s" % rssis)
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
                        bts.change_arfcn(new_arfcn)
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

"""
This controller handles two BTS units and detects interference on a channel
either is using.
"""
class DualController(Controller):
    def __init__(self, bts1_conf, bts2_conf, nct, sleep, max_delta, gsmwsdb, loglvl=logging.DEBUG):
        """
        A BTS config dictionary has the following items:
        - db_loc: The OpenBTS.db location for this BTS
        - openbts_proc: The name of the OpenBTS process, so we can kill it if necessary
        - trans_proc: The name of the transceiver process, so we can kill it if necessary
        - bts_class: The type of BTS this is (bts.BTS or bts.OldBTS, for example)
        - stream: The stream to read from (either sys.STDIN or a gsm.command_stream)
        - start_cmd: A shell command that can properly restart this BTS
        """
        self.BTS_CONF = [bts1_conf, bts2_conf]

        self.NEIGHBOR_CYCLE_TIME = nct # seconds to wait before switching up the neighbor list
        self.SLEEP_TIME = sleep # seconds between rssi checks
        self.MAX_DELTA = max_delta # max difference in rssi measurements between ARFCNs

        self.gsmwsdb_location = gsmwsdb
        self.gsmwsdb_lock = threading.Lock()
        self.gsmwsdb = sqlite3.connect(gsmwsdb)

        self.bts_units = []

        self.loglvl = loglvl
        logging.basicConfig(format='%(asctime)s %(module)s %(funcName)s %(lineno)d %(levelname)s %(message)s', filename='/var/log/gsmws.log',level=loglvl)
        logging.warning("New DualController started.")

    def setup_bts(self):
        cycle_offset = self.NEIGHBOR_CYCLE_TIME / float(len(self.BTS_CONF))
        cycle_count = 0

        now = datetime.datetime.now()
        for conf in self.BTS_CONF:
            gsmd = decoder.GSMDecoder(conf['stream'], self.gsmwsdb_lock, self.gsmwsdb_location, loglvl=self.loglvl, decoder_id=cycle_count)
            bts = conf['bts_class'](conf['db_loc'], conf['openbts_proc'], conf['trans_proc'], self.loglvl, id_num=cycle_count)

            bts.init_decoder(gsmd)

            # set up cycle time/ignored since
            bts.ignored_since = now
            bts.last_cycle_time = now - datetime.timedelta(seconds = (cycle_count*cycle_offset + self.NEIGHBOR_CYCLE_TIME)) # keep them out of sync, but make sure they start

            self.bts_units.append(bts)
            cycle_count += 1

    def pick_new_neighbors(self, bts_id_num, testing=True):
        other_arfcns = [b.current_arfcn for b in self.bts_units if b.id_num != bts_id_num] # FIXME
        if testing:
            random_arfcns = [x.current_arfcn+10 for x in self.bts_units if x.current_arfcn!=None]
        else:
            with self.gsmwsdb_lock:
                existing = [arfcn for res in self.gsmwsdb.execute("SELECT ARFCN FROM AVAIL_ARFCN").fetchall() for arfcn in res]
            random_arfcns = random.sample([_ for _ in range(1,124) if (_ not in existing and _ not in other_arfcns)], 5 - len(other_arfcns))
        logging.info("BTS %d: Current ARFCN=%s Other ARFCNs: %s Random ARFCNs: %s" % (bts_id_num, self.bts_units[bts_id_num].current_arfcn, other_arfcns, random_arfcns))
        return other_arfcns + random_arfcns

    def __read_report(self, strength_report, reference, targets):
        """
        For the given strength report, determine which target ARFCNs differ
        from the strength of the reference ARFCN by more than (postiive)
        MAX_DELTA.
        """
        res = []
        ref_strength = strength_report[reference]
        for t in targets:
            if t not in strength_report:
                # we don't have enough info. we *could* just not hear the other
                # arfcns at all but that's unlikely.
                continue
            if strength_report[t] > ref_strength + self.MAX_DELTA:
                logging.info("we should restart %d" % t)
                res.append(t)
        return res

    def main(self):
        self.initdb() # set up the gsmws db
        self.setup_bts() # set up the BTS units

        while True:
            try:
                now = datetime.datetime.now()

                # disable ignore reports if expired
                for bts in self.bts_units:
                    if bts.decoder.ignore_reports and (now - bts.ignored_since).seconds > 120:
                        bts.decoder.ignore_reports = False

                for bts in self.bts_units:
                    logging.info("BTS %d. Reported ARFCN=%s Intended Neighbors=%s Reported Neighbors=%s" % (bts.id_num, bts.current_arfcn, sorted(bts.neighbors), sorted(bts.last_arfcns)))

                for bts in self.bts_units:
                    td = (now - bts.last_cycle_time)
                    logging.debug("BTS %d td=%s, cycle=%d" % (bts.id_num, td.seconds, self.NEIGHBOR_CYCLE_TIME))
                    if td.seconds > self.NEIGHBOR_CYCLE_TIME:
                        #try:
                        #    #new_arfcn = self.pick_new_safe_arfcn()
                        #    #bts.change_arfcn(new_arfcn) # XXX don't change, for testing
                        #except IndexError:
                        #    logging.error("Unable to pick new safe ARFCN!")
                        #    pass # just don't pick for now

                        new_neighbors = self.pick_new_neighbors(bts.id_num)
                        logging.info("New neighbors (BTS %d): %s" % (bts.id_num, new_neighbors))
                        if None in new_neighbors:
                            continue # we need to wait until we've got a list of new neighbors that includes the other ARFCNs: try next time!
                        bts.set_neighbors(new_neighbors, 16001+bts.id_num)
                        bts.decoder.ignore_reports = True
                        bts.ignored_since = now
                        bts.last_cycle_time = now

                    # continually do this so OpenBTS doesn't delete these
                    bts.set_neighbors(bts.neighbors, 16001+bts.id_num)

                    rssis = bts.decoder.rssi()
                    self.update_rssi_db(rssis)
                    logging.debug("Safe ARFCNs (BTS %d): %s" % (bts.id_num, str(self.safe_arfcns())))

                # compare the BTS readings
                current_arfcns = [b.current_arfcn for b in self.bts_units]
                to_restart = set()
                for bts in self.bts_units:
                    for report in bts.reports:
                        r = self.__read_report(report, bts.current_arfcn, current_arfcns)
                        if r:
                            to_restart |= set(r)

                # kill what needs to be killed
                for bts in self.bts_units:
                    if bts.current_arfcn in to_restart:
                        #new_arfcn = self.pick_new_safe_arfcn()
                        #bts.change_arfcn(new_arfcn, True)
                        bts.change_arfcn(bts.current_arfcn + 10, True)

                time.sleep(self.SLEEP_TIME)
            except KeyboardInterrupt:
                break

"""
This controller uses two BTS units to implement handover-based scanning.
"""
class HandoverController(Controller):
    def __init__(self, bts1_conf, bts2_conf, nct, sleep, max_delta, gsmwsdb, loglvl=logging.DEBUG):
        """
        A BTS config dictionary has the following items:
        - db_loc: The OpenBTS.db location for this BTS
        - openbts_proc: The name of the OpenBTS process, so we can kill it if necessary
        - trans_proc: The name of the transceiver process, so we can kill it if necessary
        - bts_class: The type of BTS this is (bts.BTS or bts.OldBTS, for example)
        - stream: The stream to read from (either sys.STDIN or a gsm.command_stream)
        - start_cmd: A shell command that can properly restart this BTS
        """
        self.BTS_CONF = [bts1_conf, bts2_conf]

        self.NEIGHBOR_CYCLE_TIME = nct # seconds to wait before switching up the neighbor list
        self.SLEEP_TIME = sleep # seconds between rssi checks
        self.MAX_DELTA = max_delta # max difference in rssi measurements between ARFCNs

        self.gsmwsdb_location = gsmwsdb
        self.gsmwsdb_lock = threading.Lock()
        self.gsmwsdb = sqlite3.connect(gsmwsdb)

        self.bts_units = []

        self.loglvl = loglvl
        logging.basicConfig(format='%(asctime)s %(module)s %(funcName)s %(lineno)d %(levelname)s %(message)s', filename='/var/log/gsmws.log',level=loglvl)
        logging.warning("New DualController started.")

    def setup_bts(self):
        cycle_offset = self.NEIGHBOR_CYCLE_TIME / float(len(self.BTS_CONF))
        cycle_count = 0

        now = datetime.datetime.now()
        for conf in self.BTS_CONF:
            gsmd = decoder.GSMDecoder(conf['stream'], self.gsmwsdb_lock, self.gsmwsdb_location, loglvl=self.loglvl, decoder_id=cycle_count)
            bts = conf['bts_class'](conf['db_loc'], conf['openbts_proc'], conf['trans_proc'], self.loglvl, id_num=cycle_count, start_time=(now+datetime.timedelta(seconds=90*cycle_count)))

            if not bts.offset_correct:
                raise ValueError("Non-default TRX.RadioFrequencyOffset, verify radios are properly configured.")

            bts.init_decoder(gsmd)

            # set up cycle time/ignored since
            bts.ignored_since = now
            bts.last_cycle_time = now - datetime.timedelta(seconds = (cycle_count*cycle_offset + self.NEIGHBOR_CYCLE_TIME)) # keep them out of sync, but make sure they start

            self.bts_units.append(bts)
            cycle_count += 1

    def pick_new_neighbors(self, bts_id_num, testing=True):
        other_arfcns = [b.current_arfcn for b in self.bts_units if b.id_num != bts_id_num] # FIXME
        if testing:
            random_arfcns = [x.current_arfcn+10 for x in self.bts_units if x.current_arfcn!=None]
        else:
            with self.gsmwsdb_lock:
                existing = [arfcn for res in self.gsmwsdb.execute("SELECT ARFCN FROM AVAIL_ARFCN").fetchall() for arfcn in res]
            random_arfcns = random.sample([_ for _ in range(1,124) if (_ not in existing and _ not in other_arfcns)], 5 - len(other_arfcns))
        logging.info("BTS %d: Current ARFCN=%s Other ARFCNs: %s Random ARFCNs: %s" % (bts_id_num, self.bts_units[bts_id_num].current_arfcn, other_arfcns, random_arfcns))
        return other_arfcns + random_arfcns

    def main(self):
        self.initdb() # set up the gsmws db
        self.setup_bts() # set up the BTS units

        restarted = False

        while True:
            try:
                now = datetime.datetime.now()

                # disable ignore reports if expired
                for bts in self.bts_units:
                    if bts.decoder.ignore_reports and (now - bts.ignored_since).seconds > 120:
                        bts.decoder.ignore_reports = False

                for bts in self.bts_units:
                    logging.info("BTS %d. Reported ARFCN=%s Intended Neighbors=%s Reported Neighbors=%s" % (bts.id_num, bts.current_arfcn, sorted(bts.neighbors), sorted(bts.last_arfcns)))

                for bts in self.bts_units:
                    """
                    For this test, we only care about monitoring the
                    pre-defined ARFCN on which we're running our second C0 and
                    on which the primary BTS will run. We keep these all pretty
                    close together so we can have everything show up on our
                    spectrum analyzer, which only has 5MHz of usable
                    bandwidth...

                    We proceed as follows. Every iteration, we update the
                    txatten based on our warbling frequency algorithm defined
                    in bts.py. We keep track of measurement reports we get
                    back. If we receive a report exceeding threshold T for a
                    BTS that's off, we assume we've got interference on that
                    BTS. So, we shut it down, and move to a different arfcn.
                    This shouldn't affect anyone, since there shouldn't be
                    calls on it.

                    For the experiment, BTS0 is on ARFCN 20 (939.0), BTS1 and
                    the primary are on ARFCN 30 (941.0). After detecting the
                    primary, we switch BTS1 to ARFCN 40 (943.0). We artificially
                    constrain this just to keep everything on the same figure;
                    we could change frequencies arbitrarily.
                    """
                    bts.next_atten_state() # start updating the power levels for the bts units

                    # this block is where we set new neighbors and stuff
                    td = (now - bts.last_cycle_time)
                    logging.debug("BTS %d td=%s, cycle=%d" % (bts.id_num, td.seconds, self.NEIGHBOR_CYCLE_TIME))
                    if td.seconds > self.NEIGHBOR_CYCLE_TIME:
                        if bts.id_num == 0:
                            new_neighbors = [30, 40]
                        else:
                            new_neighbors = [20, 40]
                        logging.info("New neighbors (BTS %d): %s" % (bts.id_num, new_neighbors))

                        neighbor_port = 16002 if bts.id_num==0 else 16001
                        bts.set_neighbors(new_neighbors, neighbor_port, num_real=1)
                        bts.decoder.ignore_reports = True
                        bts.ignored_since = now
                        bts.last_cycle_time = now

                    # continually do this so OpenBTS doesn't delete these
                    if restarted:
                        bts.set_neighbors(bts.neighbors, neighbor_port, num_real=1)

                    rssis = bts.decoder.rssi()
                    self.update_rssi_db(rssis)
                    logging.debug("Safe ARFCNs (BTS %d): %s" % (bts.id_num, str(self.safe_arfcns())))

                # check each BTS's reports. If we find a report that exceeds MAX_DELTA for an off BTS in it, then we need to restart that BTS.
                to_restart = set()

                arfcn_to_bts = dict(zip([b.current_arfcn for b in self.bts_units], [b for b in self.bts_units]))
                reports = []
                for bts in self.bts_units:
                    reports += bts.reports

                for r in reports:
                    for t in r:
                        if t in arfcn_to_bts:
                            logging.debug("Report bts %d (ARFCN %s) is_off=%s report=%d" % (arfcn_to_bts[t].id_num, t, arfcn_to_bts[t].is_off(), r[t]))
                            if r[t] > 10 and arfcn_to_bts[t].is_off(): # 10 is a good threshold... could be set lower, but w/e
                                to_restart |= set([arfcn_to_bts[t],])

                logging.info("to_restart: %s" % (to_restart))
                # kill what needs to be killed
                for bts in to_restart:
                    bts.change_arfcn(bts.current_arfcn + 10, True)

                time.sleep(self.SLEEP_TIME)
            except KeyboardInterrupt:
                break
