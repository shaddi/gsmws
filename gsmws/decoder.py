import gsm
import collections
import threading
import logging

class GSMDecoder(threading.Thread):
    def __init__(self, stream, maxlen=100, loglvl=logging.INFO):
        threading.Thread.__init__(self)
        self.stream = stream
        self.current_message = ""
        self.current_arfcn = None
        self.last_arfcns = []
        self.ncc_permitted = None
        self.ignore_reports = False # ignore measurement reports
        self.msgs_seen = 0

        # TODO: keep these in stable storage (we lose history on every restart now)
        self.strengths_maxlen = maxlen
        self.max_strengths = {} # max strength ever seen for a given arfcn
        self.recent_strengths = {} # last 100 measurement reports for each arfcn

        logging.basicConfig(format='%(asctime)s %(module)s %(funcName)s %(lineno)d %(levelname)s %(message)s', filename='/var/log/gsmws.log',level=loglvl)

    def rssi(self):
        # returns a dict with a weighted average of each arfcn
        # we base this only on last known data for an ARFCN -- lack of report
        # doesn't mean anything, but if an arfcn is in the neighbor list and we
        # don't get a report for it, we count that as -1.
        res = {}
        for arfcn in self.max_strengths:
            tot = self.max_strengths[arfcn] + sum(self.recent_strengths[arfcn])
            res[arfcn] = float(tot) / (1 + len(self.recent_strengths[arfcn]))
        return res


    def run(self):
        for line in self.stream:
            if line.startswith("    "):
                #print "appending"
                self.current_message += "%s" % line
            else:
                self.process(self.current_message)
                self.current_message = line

    def process(self, message):
        self.msgs_seen += 1
        if message.startswith("GSM A-I/F DTAP - Measurement Report"):
            if self.ignore_reports or self.current_arfcn is None or len(self.last_arfcns) == 0:
                return # skip for now, we don't have enough data to work with

            report = gsm.MeasurementReport(self.last_arfcns, self.current_arfcn, message)
            if report.valid:
                logging.info("MeasurementReport: " + str(report))
                for arfcn in report.current_strengths:
                    if arfcn not in self.max_strengths or report.current_strengths[arfcn] > self.max_strengths[arfcn]:
                        self.max_strengths[arfcn] = report.current_strengths[arfcn]
                    if arfcn in self.recent_strengths:
                        self.recent_strengths[arfcn].append(report.current_strengths[arfcn])
                    else:
                        self.recent_strengths[arfcn] = collections.deque([report.current_strengths[arfcn]],maxlen=self.strengths_maxlen)
        elif message.startswith("GSM CCCH - System Information Type 2"):
            sysinfo2 = gsm.SystemInformationTwo(message)
            self.last_arfcns = sysinfo2.arfcns
            self.ncc_permitted = sysinfo2.ncc_permitted
            logging.debug("SystemInformation2: %s" % str(sysinfo2.arfcns))
        elif message.startswith("GSM TAP Header"):
            gsmtap = gsm.GSMTAP(message)
            self.current_arfcn = gsmtap.arfcn
            logging.debug("GSMTAP: Current ARFCN=%s" % str(gsmtap.arfcn))


if __name__ == "__main__":
    import sys
    import timeit

    gsmd = GSMDecoder(sys.stdin)

    duration = timeit.timeit(gsmd.run, number=1)
    print "Processed %d headers in %.4f seconds (%.2f msgs/sec)" % (gsmd.msgs_seen, duration, gsmd.msgs_seen / duration)
