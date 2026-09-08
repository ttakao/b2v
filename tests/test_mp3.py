import shutil
import wave
from unittest.mock import patch
import pytest
from b2v.catalog import Catalog
from b2v.audio_workflow import AudioWorkflow
from b2v.mp3 import MP3Workflow


def setup(tmp_path):
    catalog=Catalog(tmp_path)
    AudioWorkflow(catalog)
    ident='a'*32
    folder=catalog.folder(ident)/'audio'
    folder.mkdir(parents=True)
    (folder/'chunks').mkdir()
    with wave.open(str(folder/'book.wav'),'wb') as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(22050);out.writeframes(b'\0\0'*22050)
    catalog.save_doc({'id':ident,'name':'試験.pdf','busy':None,'assets':{'audio':True},'audio_result':{'filename':'book.wav'}})
    return catalog,ident,folder,MP3Workflow(catalog)


@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='ffmpeg required')
def test_conversion_cleanup(tmp_path):
    catalog,ident,folder,workflow=setup(tmp_path)
    workflow.generate(ident,{'filename':'book.wav'})
    doc=catalog.doc(ident)
    assert doc['mp3_run']['status']=='completed'
    assert doc['mp3_result']['bitrate_kbps']==96
    assert (folder/doc['mp3_result']['filename']).is_file()
    assert not (folder/'book.wav').exists()
    assert not (folder/'chunks').exists()
    assert 'audio_result' not in doc
    assert not any('hash' in k for k in doc['mp3_result'])
    workflow.delete(ident)
    with pytest.raises(FileNotFoundError):catalog.doc(ident)


def test_failure_preserves_previous(tmp_path):
    catalog,ident,folder,workflow=setup(tmp_path)
    (folder/'old.mp3').write_bytes(b'old')
    doc=catalog.doc(ident);doc['mp3_result']={'filename':'old.mp3'};catalog.save_doc(doc)
    with patch('b2v.mp3.command',side_effect=ValueError('test failure')):
        workflow.generate(ident,{'filename':'book.wav'})
    assert (folder/'old.mp3').read_bytes()==b'old'
    assert (folder/'book.wav').exists()
    assert catalog.doc(ident)['mp3_result']['filename']=='old.mp3'
    assert catalog.doc(ident)['mp3_run']['status']=='failed'


def test_registration_failure_preserves_input(tmp_path):
    catalog,ident,folder,workflow=setup(tmp_path)
    original=catalog.save_doc
    def fail_completed(doc):
        if doc.get('mp3_result'):raise OSError('database unavailable')
        original(doc)
    with patch.object(catalog,'save_doc',side_effect=fail_completed):
        workflow.generate(ident,{'filename':'book.wav'})
    assert (folder/'book.wav').exists()
    assert not catalog.doc(ident).get('mp3_result')


def test_missing_tools_and_busy(tmp_path):
    catalog,ident,folder,workflow=setup(tmp_path)
    with patch('b2v.mp3.shutil.which',return_value=None):
        with pytest.raises(ValueError,match='インストール'):workflow.start(ident)
    doc=catalog.doc(ident);doc['busy']='WAV';catalog.save_doc(doc)
    with pytest.raises(ValueError):workflow.start(ident)


def test_corrupt_input(tmp_path):
    catalog,ident,folder,workflow=setup(tmp_path)
    (folder/'book.wav').write_bytes(b'broken')
    workflow.generate(ident,{'filename':'book.wav'})
    assert '入力WAV破損' in catalog.doc(ident)['mp3_run']['error']
    assert (folder/'book.wav').exists()


def test_delete_db_failure_restores_file(tmp_path):
    catalog,ident,folder,workflow=setup(tmp_path)
    (folder/'old.mp3').write_bytes(b'old')
    doc=catalog.doc(ident);doc['mp3_result']={'filename':'old.mp3'};catalog.save_doc(doc)
    with patch.object(catalog,'save_doc',side_effect=OSError('db')):
        with pytest.raises(OSError):workflow.delete(ident)
    assert (folder/'old.mp3').read_bytes()==b'old'
    assert catalog.doc(ident)['mp3_result']['filename']=='old.mp3'
