#!/usr/bin/python

import argparse
import time
import sys

from jenkinsapi.jenkins import Jenkins
from jenkinsapi.utils.requester import Requester

from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy import desc

colors = {"SUCCESS": "#0d941d", "FAILURE": "#FF0000", "ABORTED": "#000000"}
Base = declarative_base()


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True)
    dt = Column(DateTime)
    name = Column(String)
    duration = Column(Integer)
    gerrit_ref = Column(String)
    log_path = Column(String)
    zuul_ref = Column(String)
    status = Column(String)
    url = Column(String)

job_names = ['check-tripleo-seed-precise',
             'check-tripleo-undercloud-precise',
             'check-tripleo-overcloud-precise']


def get_data(stop_after):
    for jenkinsnumber in range(1, 8):
        jurl = 'https://jenkins%02d.openstack.org' % jenkinsnumber
        jrequester = Requester(None, None, baseurl=jurl, ssl_verify=False)
        jenkins = Jenkins(jurl, requester=jrequester)

        for jobname in job_names:
            job = jenkins.get_job(jobname)
            builds = job.get_build_dict().items()
            builds.sort()
            builds.reverse()
            numhits = 0
            for buildnumber, buildurl in builds:
                build = job.get_build(buildnumber)
                gerrit_ref = zuul_ref = log_path = None
                for param in build.get_actions()['parameters']:
                    if param['name'] == "ZUUL_CHANGE_IDS":
                        gerrit_ref = param["value"]
                    elif param['name'] == "ZUUL_REF":
                        zuul_ref = param["value"]
                    elif param['name'] == "LOG_PATH":
                        log_path = param["value"]
                if zuul_ref is None or gerrit_ref is None or log_path is None:
                    continue
                thisjob = session.query(Job).filter(Job.url == buildurl).all()
                print "Checking", buildurl
                if not build.get_status():
                    continue
                if thisjob:
                    numhits += 1
                    # If we hit x jenkins jobs that we previously had,
                    # stop checking and assume we have the rest
                    if numhits >= stop_after:
                        break
                    continue
                print "Saving", buildurl
                session.add(Job(dt=build.get_timestamp(),
                                duration=build.get_duration().total_seconds(),
                                status=build.get_status(),
                                name=jobname,
                                gerrit_ref=gerrit_ref,
                                log_path=log_path,
                                zuul_ref=zuul_ref,
                                url=buildurl))
                time.sleep(1)
            session.commit()


def gen_html(html_file):
    refs_done = []
    fp = open(html_file, "w")
    fp.write('<html><head/><body><table border="1">')
    fp.write("<tr><td/>")
    for job_name in job_names:
        fp.write("<td><b>%s</b></td>" % job_name)
    fp.write("</tr>")
    count = 0
    for job in session.query(Job).order_by(desc(Job.dt)):
        if count > 500:
            break
        if job.zuul_ref in refs_done:
            continue
        refs_done.append(job.zuul_ref)

        job_columns = ""
        for job_name in job_names:
            job_columns += "<td>"
            for job in session.query(Job).\
                    filter(Job.zuul_ref == job.zuul_ref).\
                    filter(Job.name == job_name).\
                    order_by(desc(Job.dt)).all():

                color = colors.get(job.status, "#999999")
                job_columns += '<font color="%s">' % color
                job_columns += '<a STYLE="color : %s" href="%s">%s</a>' % \
                               (color, job.url, job.dt)
                job_columns += ' -- %.2f minutes ' % (job.duration / 60)
                job_columns += '<a STYLE="text-decoration:none" '
                job_columns += 'href="http://logs.openstack.org/%s">log</a>' %\
                               job.log_path
                job_columns += '</font><br/>'
            job_columns += "</td>"
        fp.write("<tr><td>")
        fp.write("<a href=\"https://review.openstack.org/#/c/%s\">%s</a></td>"
                 % (job.gerrit_ref.replace(",", "/"), job.gerrit_ref))
        fp.write(job_columns)
        fp.write("</tr>")
    fp.write("<table></body></html>")


def main(args=sys.argv[1:]):
    parser = argparse.ArgumentParser(
        description=("Get details of tripleo ci jobs and generates a html "
                     "report."))
    parser.add_argument('-f', action='store_true',
                        help='Fetch recent data from jenkins servers.')
    parser.add_argument('-n', type=int, default=3,
                        help='Stop processing downloading from jenkins after '
                             'hitting this number of builds already in the '
                             'database i.e. assume we already have the rest.')
    parser.add_argument('-o', default="tripleo-jobs.html", help="html file")
    parser.add_argument('-d', default="tripleo-jobs.db", help="sqlite file")
    opts = parser.parse_args(args)

    engine = create_engine('sqlite:///%s' % opts.d)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    global session
    session = Session()

    if opts.f:
        get_data(opts.n)
    gen_html(opts.o)

if __name__ == '__main__':
    exit(main())
