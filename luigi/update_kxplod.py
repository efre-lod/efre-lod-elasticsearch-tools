# usage for debug:
# PYTHONPATH="$PYTHONPATH:." luigi --module update_lod LODUpdate --local-scheduler

import json
from datetime import datetime, date, timedelta
from time import sleep
import os
import gzip

from es2json import esidfilegenerator
from es2json import eprint

import luigi
import luigi.contrib.esindex
from gluish.task import BaseTask
from gluish.utils import shellout


class LODKXPTask(BaseTask):
    """
    Just a base class for LOD
    -initializes the Base for all Luigi Task
    -loads configuration
    -calculates which days are missing and should be updated from the directoy, where /data1/ongoing/kxp/kxp_source/lftp_kxp.pl copies the data to
    """
    files = set()
    with open('lodkxp_config.json') as data_file:
        config = json.load(data_file)
    PPNs = []
    TAG = 'lodkxp'
    yesterday = date.today() - timedelta(1)
    lastupdate = datetime.strptime(config.get("lastupdate"), "%y%m%d")
    span = yesterday-lastupdate.date()
    for source in config["path"]:
        for i in range(span.days+1):
            day = (lastupdate+timedelta(days=i)).strftime("%y%m%d")
            if os.path.isfile("{path}/{prefix}{date}.tar.gz".format(path=source["path"], prefix=source["prefix"],date=day)):
                files.add("{path}/{prefix}{date}.tar.gz".format(path=source["path"], prefix=source["prefix"],date=day))

    def closest(self):
        return daily(date=self.date)


class LODKXPCopy(LODKXPTask):

    def run(self):
        """
        copies per day in config["dates"] the day delta to the working directoy
        """
        for fd in self.files:
            cmdstring = "cp {path} ./".format(path=fd)
            try:
                shellout(cmdstring)
            except Exception as e:
                eprint(e)
        return 0

    def complete(self):
        """
        checks whether all the day deltas which are needed are there
        """
        for fd in self.files:
            if not os.path.exists("{f}".format(f=fd.split("/")[-1])):
                return False
        return True


class LODKXPExtract(LODKXPTask):

    def requires(self):
        """
        requires LODKXPCopy
        """
        return LODKXPCopy()

    def run(self):
        """
        iterates over the days stated in LODKXPTask
        extracts the daily deltas according to the days
        returns a marc.gz file for the title data and one for the local data
        """
        for fd in self.files:
            if os.path.exists("{fd}".format(fd=fd)):
                cmdstring = "tar xvzf {fd} && cat *-tit.mrc | gzip >> {yesterday}-tit.mrc.gz && cat *-lok.mrc | gzip >> {yesterday}-lok.mrc.gz && rm *.mrc".format(fd=fd, yesterday=self.yesterday.strftime("%y%m%d"))
                shellout(cmdstring)
        return 0

    def output(self):
        """
        returns the title data 
        """
        return luigi.LocalTarget("{yesterday}-tit.mrc.gz".format(yesterday=self.yesterday.strftime("%y%m%d")))


class LODKXPTransform2ldj(LODKXPTask):

    def requires(self):
        """
        requires LODKXPExtract
        """
        return LODKXPExtract()

    def run(self):
        """
        concatenates the marc.gz title data, transforms it to line-delimited json marc
        fixes the MARC PPN  from e.g. 001: ["123ImAMarcID"] to 001:"123ImAMarcID"
        same for local data

        """
        for typ in ["tit", "lok"]:
            cmdstring = "zcat {date}-{typ}.mrc.gz | ~/git/efre-lod-elasticsearch-tools/helperscripts/marc2jsonl.py  | ~/git/efre-lod-elasticsearch-tools/helperscripts/fix_mrc_id.py | gzip > {date}-{typ}.ldj.gz".format(
                **self.config, typ=typ, date=self.yesterday.strftime("%y%m%d"))
            shellout(cmdstring)
        with open("{date}-lok-ppns.txt".format(**self.config, date=self.yesterday.strftime("%y%m%d")), "w") as outp, gzip.open("{date}-lok.ldj.gz".format(**self.config, date=self.yesterday.strftime("%y%m%d")), "rt") as inp:
            for rec in inp:
                print(json.loads(rec).get("001"), file=outp)
        return 0

    def output(self):
        """
        returns a list of PPNS of the local data as TXT
        """
        return luigi.LocalTarget("{date}-lok-ppns.txt".format(**self.config, date=self.yesterday.strftime("%y%m%d")))


class LODKXPFillRawdataIndex(LODKXPTask):
    """
    Loads raw data into a given ElasticSearch index (with help of esbulk)
    """

    def requires(self):
        """
        requires LODKXPTransform2ldj
        """
        return LODKXPTransform2ldj()

    def run(self):
        """
        fills the local data index and the source title data index with the data transformed in LODKXPTransform2ldj
        """
        for typ in ["tit", "lok"]:
            # put_dict("{rawdata_host}/kxp-{typ}".format(**self.config,typ=typ,date=self.yesterday.strftime("%y%m%d")),{"mappings":{"mrc":{"date_detection":False}}})
            # put_dict("{rawdata_host}/kxp-{typ}/_settings".format(**self.config,typ=typ,date=self.yesterday.strftime("%y%m%d")),{"index.mapping.total_fields.limit":5000})
            cmd = "esbulk -z -verbose -server {rawdata_host} -w {workers} -index kxp-{typ} -type mrc -id 001 {date}-{typ}.ldj.gz""".format(
                **self.config, typ=typ, date=self.yesterday.strftime("%y%m%d"))
            shellout(cmd)

    def complete(self):
        es_recordcount = 0
        file_recordcount = 0
        es_ids = set()
        for record in esidfilegenerator(host="{rawdata_host}".format(**self.config).rsplit("/")[-1].rsplit(":")[0],
                                        port="{rawdata_host}".format(
                                            **self.config).rsplit("/")[-1].rsplit(":")[1],
                                        index="kxp-lok".format(
                                            date=self.yesterday.strftime("%y%m%d")),
                                        type="mrc", idfile="{date}-lok-ppns.txt".format(**self.config, date=self.yesterday.strftime("%y%m%d")),
                                        source="False"):
            es_ids.add(record.get("_id"))
        es_recordcount = len(es_ids)

        try:
            with gzip.open("{date}-lok.ldj.gz".format(**self.config, date=self.yesterday.strftime("%y%m%d")), "rt") as fd:
                ids = set()
                for line in fd:
                    jline = json.loads(line)
                    ids.add(jline.get("001"))
            file_recordcount = len(ids)
        except FileNotFoundError:
            return False

        if es_recordcount == file_recordcount and es_recordcount > 0:
            return True
        return False


class LODKXPMerge(LODKXPTask):
    def requires(self):
        """
        requires LODKXPFillRawdataIndex.complete()
        """
        return LODKXPFillRawdataIndex()

    def run(self):
        """
        iterates over the id-file from LODKXPTransform2ldj, searches for the right titledata (de-14) and merges them with the merge_lok_with_tit script. finally, the data gets loaded into the kxp-de14 index
        """
        shellout(""". ~/git/efre-lod-elasticsearch-tools/init_environment.sh && ~/git/efre-lod-elasticsearch-tools/helperscripts/merge_lok_with_tit.py -selectbody \'{{\"query\": {{\"match\": {{\"852.__.a.keyword\": \"DE-14\"}}}}}}\' -title_server {rawdata_host}/kxp-tit/mrc -local_server {rawdata_host}/kxp-lok/mrc -idfile {date}-lok-ppns.txt | tee data.ldj | esbulk -server {rawdata_host} -index kxp-de14 -type mrc -id 001 -w 1 -verbose && jq -rc \'.\"001\"' data.ldj && rm data.ldj""", rawdata_host=self.config.get(
            "rawdata_host"), date=self.yesterday.strftime("%y%m%d"))

    def complete(self):
        """
        checks whether all the records from the idfile from LODKXPTransform2ldj are in the kxp-de14 index
        """
        ids = set()
        es_ids = set()
        with open("ids.txt") as inp:
            for line in inp:
                ids.add(line.strip())
        for record in esidfilegenerator(host="{rawdata_host}".format(**self.config).rsplit("/")[-1].rsplit(":")[0],
                                        port="{rawdata_host}".format(
                                            **self.config).rsplit("/")[-1].rsplit(":")[1],
                                        index="kxp-de14", type="mrc", idfile="ids.txt", source=False):
            es_ids.add(record.get("_id"))
        if len(es_ids) == len(ids) and len(es_ids) > 0:
            return True
        return False


class LODKXPProcessFromRdi(LODKXPTask):
    def requires(self):
        """
        requires LODKXPMerge.complete()
        """
        return LODKXPFillRawdataIndex()

    def run(self):
        """
        iterates over the id-file from LODKXPTransform2ldj, gets this set of records from the kxp-de14 index, transforms them to JSON-Linked-Data
        """
        # delete("{rawdata_host}/kxp-tit-{date}".format(**self.config,date=self.yesterday.strftime("%y%m%d")))
        # delete("{rawdata_host}/kxp-lok-{date}".format(**self.config,date=self.yesterday.strftime("%y%m%d")))
        cmd = "esmarc  -z -server {rawdata_host}/kxp-de14/mrc -idfile ids.txt -prefix {date}-kxp".format(
            **self.config, date=self.yesterday.strftime("%y%m%d"))
        shellout(cmd)
        sleep(5)

    def complete(self):
        """
        checks whether all the data is in the file
        """
        path = "{date}-kxp".format(date=self.yesterday.strftime("%y%m%d"))
        try:
            for index in os.listdir(path):
                for f in os.listdir(path+"/"+index):
                    if not os.path.isfile(path+"/"+index+"/"+f):
                        return False
        except FileNotFoundError:
            return False
        return True


class LODKXPUpdate(LODKXPTask):
    def requires(self):
        """
        requires LODKXPProcessFromRdi.complete()
        """
        return LODKXPProcessFromRdi()

    def run(self):
        """
        ingests the data processed in LODKXPProcessFromRdi into an elasticsearch-index
        saves the date of the update into the config file
        """
        path = "{date}-kxp".format(date=self.yesterday.strftime("%y%m%d"))
        for index in os.listdir(path):
            # doing several enrichment things before indexing the data
            for f in os.listdir(path+"/"+index):
                cmd = "esbulk -z -verbose -server {host} -w {workers} -index {index} -type schemaorg -id identifier {fd}".format(
                    **self.config, index=index, fd=path+"/"+index+"/"+f)
                shellout(cmd)
        newconfig = None
        with open('lodkxp_config.json') as data_file:
            newconfig = json.load(data_file)
        newconfig["lastupdate"] = str(self.yesterday.strftime("%y%m%d"))
        with open('lodkxp_config.json', 'w') as data_file:
            json.dump(newconfig, data_file)

    def output(self):
        return luigi.LocalTarget(path=self.path())

    def complete(self):
        path = "{date}-kxp".format(date=self.yesterday.strftime("%y%m%d"))
        ids = set()
        if not os.path.exists(path):
            return False
        for index in os.listdir(path):
            for f in os.listdir(path+"/"+index):
                with gzip.open("{fd}".format(fd=path+"/"+index+"/"+f), "rt") as inp:
                    for line in inp:
                        ids.add(json.loads(line).get("identifier"))
                cmd = "zcat {fd} | jq -rc .identifier >> schemaorg-ids-{date}.txt".format(
                    fd=path+"/"+index+"/"+f, date=self.yesterday.strftime("%y%m%d"))
                shellout(cmd)
        es_ids = set()
        for record in esidfilegenerator(host="{host}".format(**self.config).rsplit("/")[-1].rsplit(":")[0],
                                        port="{host}".format(
                                            **self.config).rsplit("/")[-1].rsplit(":")[1],
                                        index="resources", type="schemaorg", idfile="schemaorg-ids-{date}.txt".format(date=self.yesterday.strftime("%y%m%d")),
                                        source=False):
            es_ids.add(record.get("_id"))
        if len(es_ids) == len(ids) and len(es_ids) > 0:
            return True
        return False
