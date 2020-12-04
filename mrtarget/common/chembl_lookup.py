from future import standard_library
standard_library.install_aliases()
from builtins import range
from builtins import object
import functools
import logging

import itertools
import shelve
import sys#for python3 the module name has changed	import dbm
if sys.version_info >= (3, 0):	
    import dbm
    from builtins import str
else:	
    import anydbm as dbm

import tempfile
from elasticsearch_dsl import Search
from elasticsearch_dsl.query import Match

from opentargets_urlzsource import URLZSource
import simplejson as json
import unicodedata

class ChEMBLLookup(object):
    def __init__(self, target_uri, mechanism_uri, component_uri, protein_uri, 
            molecule_uri):
        super(ChEMBLLookup, self).__init__()
        self._logger = logging.getLogger(__name__)
        
        #save configuration locally for future use
        self.target_uri = target_uri
        self.mechanism_uri = mechanism_uri
        self.component_uri = component_uri
        self.protein_uri = protein_uri
        self.molecule_uri = molecule_uri

        self.protein_class = dict()
        self.target_component = dict()
        self.mechanisms = {}
        self.target2molecule = {}
        self.disease2molecule = {}
        self.targets = {}
        self.uni2chembl = {}
        self.molecule2synonyms = {}
        self.protein_classification = {}
        self.protein_class = {}
        self.protein_class_label_to_id = {}
        self.molecules_dict = self.populate_molecules_dict()

    '''
    To remove when Py3 will be the default language
    '''
    def str_hook(self, value):
        new_value = value
        if not isinstance(value, str):
            new_value = unicodedata.normalize('NFKD', value).encode('ascii','ignore')

        assert isinstance(new_value, str)
        return new_value
    '''
    Internal function to populate a dictionary like object on creation
    '''
    def populate_molecules_dict(self):
        # Shelve creates a file with specific database. Using a temp file requires a workaround to open it.
        t_filename = tempfile.NamedTemporaryFile(delete=True).name
        # dbm could not work: Eg. dbm.error: cannot add item.
        # Use dumbdbm for the local execution. Python 3 should fix this issue.
        dumb_dict = dbm.open(t_filename, 'n')
        shelve_out = shelve.Shelf(dict=dumb_dict)
        for uri in self.molecule_uri:
            self._logger.debug('ChEMBL getting Molecule from %s', uri)
            with URLZSource(uri).open() as f_obj:
                for line in f_obj:
                    #TODO handle malformed JSON lines better
                    mol = json.loads(line)
                    shelve_out[str(mol["molecule_chembl_id"])] = mol

        self._logger.debug('ChEMBL Molecule loading done.')
        return shelve_out


    def download_molecules_linked_to_target(self):
        '''generate a dictionary with all the synonyms known for a given molecules.
         Only retrieves molecules linked to a target'''

        '''fetches all the targets from chembl and store their data and a mapping to uniprot id'''

        for uri in self.target_uri:
            with URLZSource(uri).open() as f_obj:
                for line in f_obj:
                    i = json.loads(line)
                    if 'target_components' in i and \
                            i['target_components'] and \
                            'accession' in i['target_components'][0] and \
                            i['target_components'][0]['accession']:
                        uniprot_id = i['target_components'][0]['accession']
                        self.targets[uniprot_id] = i
                        self.uni2chembl[uniprot_id] = i['target_chembl_id']

        allowed_target_chembl_ids = set(self.uni2chembl.values())
        for uri in self.mechanism_uri:
            with URLZSource(uri).open() as f_obj:
                for line in f_obj:
                    i = json.loads(line)
                    self.mechanisms[i['record_id']] = i
                    target_id = i['target_chembl_id']
                    if target_id in allowed_target_chembl_ids:
                        if target_id not in self.target2molecule:
                            self.target2molecule[target_id] = set()
                        self.target2molecule[target_id].add(i['molecule_chembl_id'])

        required_molecules = set()
        self._logger.info('chembl t2m mols')
        for molecules in list(self.target2molecule.values()):
            for molecule in molecules:
                required_molecules.add(molecule)
        required_molecules = list(required_molecules)
        batch_size = 100
        self._logger.debug('chembl populate synonyms')
        for i in range(0, len(required_molecules), batch_size):
            self.populate_synonyms_for_molecule(required_molecules[i:i + batch_size],
                                                 self.molecule2synonyms)

    @staticmethod
    def __extract_protein_classes_from(uris):
        """uris is a list of filenames: str
        returns ({id:[{label:l, id:id},...],...}, {label:id,...})
        """
        classes = {}
        classes_inv_idx = {}

        for uri in uris:
            with URLZSource(uri).open() as f_obj:
                for line in f_obj:
                    i = json.loads(line)
                    protein_class_id = i.pop('protein_class_id')

                    gen = ((k, dict(label=v, id='')) for k, v in i.items() if v)
                    protein_class_data = sorted(gen, key=lambda x: x[0], reverse=True)

                    classes[protein_class_id] = protein_class_data

                    label = protein_class_data[0][1]['label']
                    classes_inv_idx[label] = protein_class_id

        '''inject missing ids'''
        for k, v in classes.items():
            for level, data in v:
                label = data['label']
                if label in classes_inv_idx:
                    data['id'] = classes_inv_idx[label]

        return classes, classes_inv_idx


    def download_protein_classification(self):
        '''fetches targets components from chembls and inject the target class data in self.protein_classification'''

        self.protein_class, self.protein_class_label_to_id = \
            self.__extract_protein_classes_from(self.protein_uri)

        for uri in self.component_uri:
            with URLZSource(uri).open() as f_obj:
                for line in f_obj:
                    i = json.loads(line)
                    if 'accession' in i:
                        if i['accession'] not in self.protein_classification:
                            self.protein_classification[i['accession']] = []
                        for classification in i['protein_classifications']:
                            protein_class_id = classification['protein_classification_id']
                            self.protein_classification[i['accession']].append(dict(self.protein_class[protein_class_id]))

    def get_molecules_from_evidence(self, es, index):

        fields = ['target.id','disease.id', 'evidence.target2drug.urls']
        for e in Search().using(es).index(index).query(
            Match(type="known_drug")).source(includes=fields).scan():
            e = e.to_dict()
            #get information from URLs that we need to extract short ids
            #e.g. https://www.ebi.ac.uk/chembl/compound/inspect/CHEMBL502835
            molecule_ids = [self.str_hook(i['url'].split('/')[-1]) for i in e['evidence']['target2drug']['urls'] if
                           '/compound/' in i['url']]
            if molecule_ids:
                molecule_id=molecule_ids[0]

                disease_id = self.str_hook(e['disease']['id'])
                target_id = self.str_hook(e['target']['id'])
                if disease_id not in self.disease2molecule:
                    self.disease2molecule[disease_id]=set()
                self.disease2molecule[disease_id].add(molecule_id)
                if target_id not in self.target2molecule:
                    self.target2molecule[target_id]=set()
                self.target2molecule[target_id].add(molecule_id)

    def populate_synonyms_for_molecule(self, molecule_set, molecules_syn_dict):
        def _append_to_mol2syn(m2s_dict, molecule):
            """if molecule has synonyms create a clean entry in m2s_dict with all synms for that chembl_id.
            Returns either None if goes ok or the molecule chembl id if something wrong"""
            if 'molecule_synonyms' in molecule and molecule['molecule_synonyms']:
                synonyms = []
                for syn in molecule['molecule_synonyms']:
                    synonyms.append(syn['synonyms'])
                    synonyms.append(syn['molecule_synonym'])
                synonyms = list(set(synonyms))
                m2s_dict[molecule['molecule_chembl_id']] = synonyms
                return None
            else:
                return molecule['molecule_chembl_id']

        if not molecule_set or not len(molecule_set):
            self._logger.warn("No molecules in set")
            return

        data = {'molecules':[]}
        for mol_k in molecule_set:
            if mol_k in self.molecules_dict:
                data['molecules'].append(self.molecules_dict[mol_k])
            else:
                raise ValueError('problem retrieving the molecule info from the local db', str(mol_k))

        #if the data is what we expected, process it
        if 'molecules' in data:
            map_f = functools.partial(_append_to_mol2syn, molecules_syn_dict)
            mols_without_syn = \
                list(itertools.filterfalse(lambda mol: mol is None, map(map_f, data['molecules'])))
            if mols_without_syn:
                self._logger.debug('molecule list with no synonyms %s', str(mols_without_syn))

        else:
            self._logger.error("there is no 'molecules' key in the structure")
            raise RuntimeError("unexpected chembl API response")


