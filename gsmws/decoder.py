import gsm
import collections
import threading
import logging
import datetime
import Queue
import sqlite3

class MeasurementReportList(object):
    def __init__(self, maxlen=10000):
        self.lock = threading.Lock()
        self.maxlen = maxlen
        self.reports = collections.deque(maxlen=maxlen)

    def put(self, report):
        with self.lock:
            self.reports.append(report)

    def get(self):
        with self.lock:
            self.reports.popleft()

    def getall(self):
        with self.lock:
            reports, self.reports = self.reports, collections.deque(maxlen=self.maxlen)
        return list(reports)


class GSMDecoder(threading.Thread):
    """
    This is responsible for managing the packet stream from tshark, processing
    reports, and storing the data.
    """
    def __init__(self, stream, db_lock, gsmwsdb_location="/tmp/gsmws.db", maxlen=100, loglvl=logging.INFO, decoder_id=0):
        threading.Thread.__init__(self)
        self.stream = stream
        self.current_message = ""
        self.current_arfcn = None
        self.last_arfcns = []
        self.ncc_permitted = None
        self.ignore_reports = False # ignore measurement reports
        self.msgs_seen = 0

        self.gsmwsdb_lock = db_lock
        self.gsmwsdb_location = gsmwsdb_location
        self.gsmwsdb = None # this gets created in run()

        self.decoder_id = decoder_id

        self.rssi_queue = Queue.Queue()

        self.reports = MeasurementReportList()

        self.strengths_maxlen = maxlen
        self.max_strengths = {} # max strength ever seen for a given arfcn
        self.recent_strengths = {} # last 100 measurement reports for each arfcn
        logging.basicConfig(format='%(asctime)s %(module)s %(funcName)s %(lineno)d %(levelname)s %(message)s', filename='/var/log/gsmws.log',level=loglvl)


    def _populate_strengths(self):
        """
        Rather than storing our history, we can just store the current mean for
        each ARFCN, plus the number of recent readings we have. On start, we
        just add N instances of each ARFCN's mean to the list. This has the
        downside of being not general (only works with means) and losing
        history potentially (i.e., we die twice in a row: we'll repopulate with
        just the mean value from before).
        """
        # populate the above from stable
        with self.gsmwsdb_lock:
            max_strengths = self.gsmwsdb.execute("SELECT ARFCN, RSSI FROM MAX_STRENGTHS").fetchall()
            for item in max_strengths:
                self.max_strengths[item[0]] = item[1]

            recent = self.gsmwsdb.execute("SELECT ARFCN, RSSI, COUNT FROM AVG_STRENGTHS").fetchall()
            for item in recent:
                self.recent_strengths[item[0]] = collections.deque([item[1] for _ in range(0,item[2])],maxlen=self.strengths_maxlen)

    def __write_rssi(self):
        if not self.rssi_queue.empty():
            with self.gsmwsdb_lock:
                while not self.rssi_queue.empty():
                    try:
                        query = self.rssi_queue.get()
                        self.gsmwsdb.execute(query[0], query[1])
                    except Queue.Empty:
                        break
                self.gsmwsdb.commit()


    def rssi(self):
        # returns a dict with a weighted average of each arfcn
        # we base this only on last known data for an ARFCN -- lack of report
        # doesn't mean anything, but if an arfcn is in the neighbor list and we
        # don't get a report for it, we count that as -1.

        res = {}
        now = datetime.datetime.now()

        for arfcn in self.max_strengths:
            tot = self.max_strengths[arfcn] + sum(self.recent_strengths[arfcn])
            res[arfcn] = float(tot) / (1 + len(self.recent_strengths[arfcn]))

            # now, update the db
            recent_avg = sum(self.recent_strengths[arfcn]) / float(len(self.recent_strengths[arfcn]))
            self.rssi_queue.put(("DELETE FROM AVG_STRENGTHS WHERE ARFCN=?", (arfcn,)))
            self.rssi_queue.put(("INSERT INTO AVG_STRENGTHS VALUES (?, ?, ?, ?)", (now, arfcn, recent_avg, len(self.recent_strengths[arfcn]))))

        return res


    def run(self):
        self.gsmwsdb = sqlite3.connect(self.gsmwsdb_location)
        self._populate_strengths()

        last_rssi_update = datetime.datetime.now()

        # Main processing loop. We read output from tshark line by line
        # breaking every time we find a line that is unindented. Unindented
        # line = new message. The message is then handed off to process(),
        # which extracts relevant information from it.
        for line in self.stream:
            self.__write_rssi()
            if line.startswith("    "):
                #print "appending"
                self.current_message += "%s" % line
            else:
                self.process(self.current_message)
                self.current_message = line

    def update_strength(self, strengths):
        self.update_max_strength(strengths)
        self.update_recent_strengths(strengths)

    def update_max_strength(self, strengths):
        with self.gsmwsdb_lock:
            for arfcn in strengths:
                value = strengths[arfcn]
                now = datetime.datetime.now()

                # FIXME potential leak here: we could record max values twice if we're
                # not in sync w/ db, but that should only happen rarely
                if arfcn not in self.max_strengths:
                    self.max_strengths[arfcn] = value
                    self.gsmwsdb.execute("INSERT INTO MAX_STRENGTHS VALUES(?,?,?)", (now, arfcn, value))
                elif value > self.max_strengths[arfcn]:
                    self.max_strengths[arfcn] = value
                    self.gsmwsdb.execute("UPDATE MAX_STRENGTHS SET TIMESTAMP=?, RSSI=? WHERE ARFCN=?", (now, value, arfcn))

            to_delete = []
            for arfcn in self.max_strengths:
                if arfcn not in strengths:
                    to_delete.append(arfcn)
                    self.gsmwsdb.execute("DELETE FROM MAX_STRENGTHS WHERE ARFCN=?", (arfcn,))
            for arfcn in to_delete:
                del self.max_strengths[arfcn]
            self.gsmwsdb.commit()



    def update_recent_strengths(self, strengths):
        for arfcn in strengths:
            value = strengths[arfcn]
            if arfcn in self.recent_strengths:
                self.recent_strengths[arfcn].append(value)
            else:
                self.recent_strengths[arfcn] = collections.deque([value],maxlen=self.strengths_maxlen)

        with self.gsmwsdb_lock:
            to_delete = []
            for arfcn in self.recent_strengths:
                if arfcn not in strengths:
                    to_delete.append(arfcn)
                    self.gsmwsdb.execute("DELETE FROM AVG_STRENGTHS WHERE ARFCN=?", (arfcn,))

            for arfcn in to_delete:
                del self.recent_strengths[arfcn]

        # force a write whenever we update strength
        self.rssi()
        self.__write_rssi()

    def process(self, message):
        self.msgs_seen += 1
        if message.startswith("GSM A-I/F DTAP - Measurement Report"):
            if self.ignore_reports or self.current_arfcn is None or len(self.last_arfcns) == 0:
                return # skip for now, we don't have enough data to work with

            report = gsm.MeasurementReport(self.last_arfcns, self.current_arfcn, message)
            if report.valid:
                logging.info("(decoder %d) MeasurementReport: " % (self.decoder_id) + str(report))
                self.reports.put(report.current_strengths)
                for arfcn in report.current_strengths:
                    self.update_max_strength(arfcn,report.current_strengths[arfcn])
                    self.update_recent_strengths(arfcn, report.current_strengths[arfcn])

                for arfcn in report.current_bsics:
                    if report.current_bsics[arfcn] != None:
                        logging.debug("ZOUNDS! AN ENEMY BSIC: %d (ARFCN %d, decoder %d)" % (report.current_bsics[arfcn], arfcn, self.decoder_id))
        elif message.startswith("GSM CCCH - System Information Type 2"):
            sysinfo2 = gsm.SystemInformationTwo(message)
            self.last_arfcns = sysinfo2.arfcns
            self.ncc_permitted = sysinfo2.ncc_permitted
            logging.debug("(decoder %d) SystemInformation2: %s" % (self.decoder_id, str(sysinfo2.arfcns)))
        elif message.startswith("GSM TAP Header"):
            gsmtap = gsm.GSMTAP(message)
            self.current_arfcn = gsmtap.arfcn
            logging.debug("(decoder %d) GSMTAP: Current ARFCN=%s" % (self.decoder_id, str(gsmtap.arfcn)))


if __name__ == "__main__":
    import sys
    import timeit

    gsmd = GSMDecoder(sys.stdin)

    duration = timeit.timeit(gsmd.run, number=1)
    print "Processed %d headers in %.4f seconds (%.2f msgs/sec)" % (gsmd.msgs_seen, duration, gsmd.msgs_seen / duration)
