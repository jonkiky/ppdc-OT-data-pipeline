import logging
import jsonpickle
import base64
import lxml.etree as etree

from mrtarget.common.UniprotIO import Parser
from opentargets_urlzsource import URLZSource
from mrtarget.common.connection import new_es_client
from mrtarget.common.esutil import ElasticsearchBulkIndexManager
import elasticsearch


"""
Generates elasticsearch action objects from the results iterator

Output suitable for use with elasticsearch.helpers 
"""
def elasticsearch_actions(entries, dry_run, index, doc):
    for entry in entries:
        #horrible hack, just save it as a blob
        json_seqrec = base64.b64encode(jsonpickle.encode(entry))

        if not dry_run:
            action = {}
            action["_index"] = index
            action["_type"] = doc
            action["_id"] = entry.id
            action["_source"] = {'entry': json_seqrec}

            yield action

def generate_uniprot(uri):
    with URLZSource(uri).open() as r_file:
        for event, elem in etree.iterparse(r_file, events=("end",), 
                tag='{http://uniprot.org/uniprot}entry'):

            #parse the XML into an object
            entry = Parser(elem, return_raw_comments=False).parse()
            elem.clear()

            yield entry

class UniprotDownloader(object):
    def __init__(self, es_hosts, es_index, es_doc, uri, workers_write, queue_write):
        self.es_hosts = es_hosts
        self.es_index = es_index
        self.es_doc = es_doc
        self.uri = uri
        self.workers_write = workers_write
        self.queue_write = queue_write

        self.logger = logging.getLogger(__name__)

    def process(self, dry_run):
        self.logger.debug("download uniprot uri %s", self.uri)
        self.logger.debug("to generate this file you have to call this url "
                            "https://www.uniprot.org/uniprot/?query=reviewed%3Ayes%2BAND%2Borganism%3A9606&compress=yes&format=xml")

        es = new_es_client(self.es_hosts)
        with ElasticsearchBulkIndexManager(es, self.es_index):

            items = generate_uniprot(self.uri)

            #write into elasticsearch
            failcount = 0
            if not dry_run:
                chunk_size = 1000 #TODO make configurable
                actions = elasticsearch_actions(items, dry_run, self.es_index, self.es_doc)
                for result in elasticsearch.helpers.parallel_bulk(es, actions,
                        thread_count=self.workers_write, queue_size=self.queue_write, 
                        chunk_size=chunk_size):
                    success, details = result
                    if not success:
                        failcount += 1

        if failcount:
            raise RuntimeError("%s failed to index" % failcount)

    def qc(self, esquery):
        """Run a series of QC tests on EFO elasticsearch index. Returns a dictionary
        of string test names and result objects
        """
        self.logger.info("Starting QC")
        #number of uniprot entries
        uniprot_count = 0
        #Note: try to avoid doing this more than once!
        for unprot_entry in esquery.get_all_uniprot_entries():
            uniprot_count += 1

            if uniprot_count % 1000 == 0:
                self.logger.debug("QC of %d uniprot entries", uniprot_count)

        #put the metrics into a single dict
        metrics = dict()
        metrics["uniprot.count"] = uniprot_count

        self.logger.info("Finished QC")
        return metrics
