#!/usr/bin/python

import datetime
import os
import re
import sys
import time
import urllib

from jenkinsapi.custom_exceptions import UnknownJob
from jenkinsapi.jenkins import Jenkins
from jenkinsapi.utils.requester import Requester
from oslo_config import cfg
from oslo_log import log as logging
import requests

from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy import desc

requests.packages.urllib3.disable_warnings()

colors = {"SUCCESS": "#0d941d", "FAILURE": "#FF0000", "ABORTED": "#000000"}
Base = declarative_base()

now = datetime.datetime.now()

default_jobs = [
    'periodic-ceilometer-docs-icehouse',
    'periodic-ceilometer-docs-juno',
    'periodic-ceilometer-docs-icehouse']

opts= [
    cfg.ListOpt('job_names',
               default=default_jobs,
               help='List of jobs to gather stats about'),
    cfg.StrOpt('job_prefix', default='periodic-ceilometer-',
               help='Prefix to remove in reports'),
    cfg.BoolOpt('fetch', default=False,
                        help='Fetch recent data from jenkins servers.'),
    cfg.IntOpt('max_builds',
               default=3,
               help='Stop processing downloading from jenkins after '
               'hitting this number of builds already in the '
               'database i.e. assume we already have the rest.'),
    cfg.StrOpt('html_output_file', default="tripleo-jobs.html", help="html file"),
    cfg.StrOpt('database_file', default="tripleo-jobs.db", help="sqlite file"),
    cfg.StrOpt('stats_hours', default=48, help="Number of hours for which "
               "stats should be gathered. Default: 48")
]

CONF = cfg.CONF
CONF.register_cli_opts(opts)
logging.register_options(CONF)


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True)
    dt = Column(DateTime)
    name = Column(String)
    duration = Column(Integer)
    gerrit_ref = Column(String)
    log_path = Column(String)
    zuul_ref = Column(String)
    zuul_project = Column(String)
    status = Column(String)
    url = Column(String)


def get_data(stop_after, session, job_names, LOG):
    for jenkinsnumber in range(1, 8):
        jurl = 'https://jenkins%02d.openstack.org' % jenkinsnumber
        jrequester = Requester(None, None, baseurl=jurl, ssl_verify=False)
        try:
            jenkins = Jenkins(jurl, requester=jrequester)
            LOG.debug('Now scraping: %s', jurl)
        except:
            LOG.warn("Couldn't connect to %s" % jurl)
            continue

        for jobname in job_names:
            try:
                job = jenkins.get_job(jobname)
            except:
                continue
            LOG.debug('Looking for job: %s', jobname)
            try:
                builds = job.get_build_dict().items()
            except:
                LOG.warn("Could not retrieve %s", jobname)
                LOG.warn(e)
                continue
            builds.sort()
            builds.reverse()
            numhits = 0
            for buildnumber, buildurl in builds:
                LOG.debug('Checking build number: %s', buildnumber)
                thisjob = session.query(Job).filter(Job.url == buildurl).all()
                # these are finised no need to hit jenkins again
                if thisjob and thisjob[0].status in ["SUCCESS","FAILURE","ABORTED"]:
                    LOG.debug('Job complete, continuing')
                    continue
                time.sleep(.3)
                LOG.info("Checking %s", buildurl)
                try:
                    build = job.get_build(buildnumber)
                except:
                    LOG.warn("Could not retrieve build %s for %s", buildnumber, jobname)
                    LOG.warn(e)
                    continue
                gerrit_ref = zuul_ref = zuul_project = log_path = None
                for param in build.get_actions()['parameters']:
                    if param['name'] == "ZUUL_CHANGE_IDS":
                        gerrit_ref = param["value"].split(" ")[-1]
                    elif param['name'] == "ZUUL_PROJECT":
                        zuul_project = param["value"]
                    elif param['name'] == "LOG_PATH":
                        log_path = param["value"]
                if log_path is None:
                    continue

                if thisjob:
                    print "Updating", buildurl
                    thisjob[0].status = build.get_status()
                    thisjob[0].duration = build.get_duration().seconds
                    continue
                print "Saving", buildurl
                session.add(Job(dt=build.get_timestamp(),
                                duration=build.get_duration().seconds,
                                status=build.get_status(),
                                name=jobname,
                                gerrit_ref=gerrit_ref,
                                log_path=log_path,
                                zuul_ref=zuul_ref,
                                zuul_project=zuul_project,
                                url=buildurl))
            session.commit()


def gen_html(html_file, stats_hours, session, job_names, job_prefix, LOG):
    LOG.info("Generating %s", html_file)
    refs_done = []
    fp = open(html_file, "w")

    if job_prefix[-1] == '-':
        job_label = job_prefix[:-1]
    else:
        job_label = job_prefix
    fp.write('<html><head/><body>')
    fp.write('<h1>%s jobs for the last %s hours</h1>' % (job_label, stats_hours))
    fp.write('Generated: %s<p/>' % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    fp.write('<table border="1">')
    fp.write("<tr><th/>")
    for job_name in job_names:
        fp.write("<th>%s</th>" % job_name.replace(job_prefix, ""))
    fp.write("</tr>")
    count = 0
    for job in session.query(Job).order_by(desc(Job.dt)):
        if count > 500:
            break
        if job.zuul_ref in refs_done:
            continue
        refs_done.append(job.zuul_ref)
        count += 1

        job_columns = ""
        # Don't need this in a few days 19/2/2015 (once jobs being reported are new format)
        this_gerrit_num = this_gerrit_ref = None
        if job.gerrit_ref:
            this_gerrit_ref = job.gerrit_ref.split(" ")[-1]
            this_gerrit_num = this_gerrit_ref.split(",")[0]
        for job_name in job_names:
            job_columns += "<td>"
            for job in session.query(Job).\
                    filter(Job.name == job_name).\
                    order_by(desc(Job.dt)).all():

                # For recent completed jobs get the logs
                if job.status in ["SUCCESS","FAILURE","ABORTED"]:
                    td = now - job.dt
                    if (td.seconds + (td.days*24*60*60)) < (stats_hours * (60*60)):
                        parse_logs('http://logs.openstack.org/%s/console.html' % job.log_path)

                color = colors.get(job.status, "#999999")
                job_columns += '<font color="%s">' % color
                job_columns += '<a STYLE="color : %s" href="%s">%s</a>' % \
                               (color, job.url, job.dt.strftime("%m-%d %H:%M"))
                job_columns += ' - %.0f min ' % (job.duration / 60)
                job_columns += ' - %s - ' % job.status
                job_columns += '<a STYLE="text-decoration:none" '
                job_columns += 'href="http://logs.openstack.org/%s">log</a>' %\
                               job.log_path
                if this_gerrit_num:
                    successes = len(session.query(Job).filter(Job.status == "SUCCESS").filter(Job.name == job_name).filter(Job.gerrit_ref.like("%s,%%" % (this_gerrit_num))).all())
                    failures = len(session.query(Job).filter(Job.status == "FAILURE").filter(Job.name == job_name).filter(Job.gerrit_ref.like("%s,%%" % (this_gerrit_num))).all())
                    job_columns += ' %d/%d' % (successes, (successes+failures))

                job_columns += '</font><br/>'
            job_columns += "</td>"
        fp.write("<tr><td>")
        project = ""
        if job.zuul_project:
            project = job.zuul_project.split("/")[-1]
        if this_gerrit_ref:
            fp.write("<a href=\"https://review.openstack.org/#/c/%s\">%s %s</a></td>"
                     % (this_gerrit_ref.replace(",", "/"), this_gerrit_ref, project))
        else:
            fp.write("</td>%s" % job_columns)
        fp.write("</tr>")
    fp.write("<table></body></html>")

def getloglinetime(line):
    return time.mktime(time.strptime(line.split(".")[0], "%Y-%m-%d %H:%M:%S"))

class Stats:
    def __init__(self):
        self.regions = []
        self.regions.append(("R1", re.compile("test-cloud-hp1-")))
        self.regions.append(("R2", re.compile("test-cloud-rh1-")))
        self.regions.append(("?", re.compile(".")))

        self.outcomes = []
        self.outcomes.append(("SUCCESS", re.compile("Finished: SUCCESS")))
        self.outcomes.append(("FAILURE", re.compile("Finished: FAILURE")))
        self.outcomes.append(("ABORTED", re.compile("Finished: ABORTED")))
        self.outcomes.append(("OTHER", re.compile(".")))

        self.stats = {}

    def addLogFile(self, url, logfile):
        print logfile
        fp = open(logfile)
        data = fp.read()
        fp.close()

        if data.strip() == "File Not Found":
            return

        for rname, matcher in self.regions:
            if not matcher.search(data):
                continue
            rstats = self.stats.setdefault(rname, {})
            rstats["COUNT"] = rstats.get("COUNT", 0) + 1

            for oname, omatcher in self.outcomes:
                if omatcher.search(data):
                    ostats = rstats.setdefault(oname, [])

                    datalist = data.split("\n")

                    runtime =  getloglinetime(datalist[-3]) - getloglinetime(datalist[1])
                    ostats.append((url, runtime))
                    break
            break

    def report(self, outfile, stats_hours):
        fp = open(outfile, "w")
        fp.write("<pre>%s : Stats for completed jobs that started in the last %d hours\n\n" % (now.strftime("%Y:%m:%d %H:%M"), stats_hours))
        fp.write("Total : %d\n" % sum([i["COUNT"] for i in self.stats.values()]))
        for r in self.regions:
            try:
                fp.write("%05s : %s\n" % (r[0], self.stats[r[0]]["COUNT"]))
            except:
                pass
        fp.write("\n")

        for r in self.regions:
            rstats = self.stats.get(r[0])
            if not rstats:
                continue
            fp.write("%s (%d)\n" % (r[0], rstats["COUNT"]))
            overcloud_successes = [f for f in rstats.get("SUCCESS", []) if "check-tripleo-ironic-overcloud-f21-nonha" in  f[0] or "check-tripleo-ironic-overcloud-precise-nonha" in  f[0]]
            overcloud_failures = [f for f in rstats.get("FAILURE", []) if "check-tripleo-ironic-overcloud-f21-nonha" in  f[0] or "check-tripleo-ironic-overcloud-precise-nonha" in  f[0]]
            overcloud_success_rate=float(len(overcloud_successes))/(len(overcloud_successes)+len(overcloud_failures)) * 100
            overcloud_runtime = 0
            if overcloud_successes:
                overcloud_runtime = sum(f[1] for f in overcloud_successes) / float(len(overcloud_successes)) / 60
            fp.write(" SUCCESS %d (%2.1f%%) (NONHA Overcloud average %0.1fmin for %d jobs, %2.1f%%)\n" % (len(rstats.get("SUCCESS", [])), float(len(rstats.get("SUCCESS", []))*100)/rstats["COUNT"], overcloud_runtime, len(overcloud_successes), overcloud_success_rate))
            fp.write(" FAILURE %d\n" % (len(rstats.get("FAILURE", []))))
            for f in rstats.get("FAILURE", []):
                fp.write("    %s\n" % f[0])
            fp.write(" ABORTED %d\n" % (len(rstats.get("ABORTED", []))))
            for f in rstats.get("ABORTED", []):
                fp.write("    %s\n" % f[0])
            fp.write(" OTHER   %d\n" % (len(rstats.get("OTHER", []))))
            for f in rstats.get("OTHER", []):
                fp.write("    %s\n" % f[0])
            fp.write("\n")
        
stats = Stats()


def parse_logs(logurl):
    if not os.path.isdir("./log"):
        os.mkdir("./log")
    logfile = os.path.join("./log", logurl.replace("/", "_").replace(":", "_"))
    if not os.path.isfile(logfile):
        urllib.urlretrieve(logurl, logfile)
    stats.addLogFile(logurl, logfile)


def main():

    CONF(sys.argv[1:])

    logging.set_defaults()
    logging.setup(CONF, 'jenkins-job-scraper')
    LOG = logging.getLogger(__name__)

    engine = create_engine('sqlite:///%s' % os.path.expanduser(CONF.database_file))
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    if CONF.fetch:
        get_data(CONF.max_builds, session=session, job_names=CONF.job_names, LOG=LOG)
    gen_html(os.path.expanduser(CONF.html_output_file), CONF.stats_hours, session=session,
             job_names=CONF.job_names, job_prefix=CONF.job_prefix, LOG=LOG)

    # stats.report("s_" + CONF.html_output_file, CONF.stats_hours)

if __name__ == '__main__':
    exit(main())
