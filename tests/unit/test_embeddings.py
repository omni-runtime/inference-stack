"""Embedding model limits survive the single-source configuration boundary."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
from stack_config import StackConfig, validate_document

class EmbeddingsConfiguration(unittest.TestCase):
    def setUp(self):
        self.doc=yaml.safe_load((ROOT/'examples/embeddings/stack.yaml').read_text())
        self.doc['artifacts']={key:str((ROOT/'examples/embeddings'/value).resolve()) for key,value in self.doc['artifacts'].items()}
    def load(self, doc):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'stack.yaml';path.write_text(yaml.safe_dump(doc))
            return StackConfig(path)
    def test_render_preserves_constraints_and_cloud_local_scopes(self):
        config=self.load(self.doc)
        models=config.models
        scopes={'auto':models,'local-only':[m for m in models if m['pool']!='cloud'],'cloud-only':[m for m in models if m['pool']=='cloud']}
        env=Environment(loader=FileSystemLoader(ROOT/'config/router'),undefined=StrictUndefined)
        result=yaml.safe_load(env.get_template('config.yaml.j2').render(models=models,scopes=scopes,video_models=[]))
        cards={m['name']:m for m in result['routing']['modelCards']}
        for model in models:self.assertEqual(cards[model['name']]['embedding'],model['embedding'])
        recipes={r['name']:r['routing']['decisions'][0]['modelRefs'] for r in result['recipes']}
        self.assertEqual(recipes['local-only'],[{'model':'local/embedding'}])
        self.assertEqual(recipes['cloud-only'],[{'model':'cloud/embedding'}])
    def test_invalid_embedding_declarations_rejected(self):
        for change in [{'embedding':None},{'embedding':{'space':'bad\nspace','dimensions':64,'max_batch_size':1}}, {'max_output_tokens':1},{'capabilities':['text']}, {'embedding':{'space':'safe','dimensions':64,'min_dimensions':128,'max_batch_size':1}}, {'api_format':'openai'}]:
            with self.subTest(change=change):
                doc=copy.deepcopy(self.doc);doc['models'][0].update(change)
                with self.assertRaises(ValueError):self.load(doc)

if __name__=='__main__':unittest.main()

class ArkDimensionsConfiguration(EmbeddingsConfiguration):
    def setUp(self):
        self.doc=yaml.safe_load((ROOT/'examples/ark-embeddings/stack.yaml').read_text())
        self.doc['artifacts']={key:str((ROOT/'examples/ark-embeddings'/value).resolve()) for key,value in self.doc['artifacts'].items()}
    def test_discrete_dimensions_survive_render(self):
        config=self.load(self.doc)
        cloud=next(m for m in config.models if m['pool']=='cloud')
        self.assertEqual(cloud['api_format'],'ark_embeddings')
        self.assertEqual(cloud['embedding']['allowed_dimensions'],[1024,2048])
    def test_invalid_discrete_dimensions(self):
        for allowed in [[1024],[1024,1024,2048],[512,2048],[2048,4096]]:
            with self.subTest(allowed=allowed):
                doc=copy.deepcopy(self.doc);doc['models'][0]['embedding']['allowed_dimensions']=allowed
                with self.assertRaises(ValueError):self.load(doc)
