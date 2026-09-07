"""Same Tesseract engine, languages and segmentation; collect TXT and TSV in one invocation."""
import os
import subprocess
import tempfile
from pathlib import Path
import cv2
from .ocr import TesseractOCR, horizontal_blocks
from .quality import parse_tsv


class ConfidenceOCR(TesseractOCR):
    def recognize_with_confidence(self, image, direction, dpi, blocks_mode=True):
        boxes = horizontal_blocks(image) if direction == 'horizontal' and blocks_mode else []
        if direction == 'horizontal' and blocks_mode:
            images = [cv2.copyMakeBorder(image[y:y+h,x:x+w],15,15,15,15,cv2.BORDER_CONSTANT,value=255) for x,y,w,h in boxes]
            lang,psm='jpn','7'
        else:
            images=[image];lang,psm=('jpn_vert','5') if direction=='vertical' else ('jpn','3')
        texts,tsvs,units=[],[],[]
        for block in images:
            ok,png=cv2.imencode('.png',block)
            if not ok: raise RuntimeError('OCR画像をエンコードできません。')
            with tempfile.TemporaryDirectory(prefix='b2v-tsv-') as temporary:
                base=Path(temporary)/'result'
                result=subprocess.run([self.binary,'stdin',str(base),'-l',lang,'--psm',psm,'--dpi',str(dpi),'txt','tsv'],input=png.tobytes(),capture_output=True,timeout=180,env={**os.environ,'OMP_THREAD_LIMIT':'2'})
                if result.returncode: raise RuntimeError(result.stderr.decode('utf-8',errors='replace')[-2000:])
                texts.append(base.with_suffix('.txt').read_text())
                tsv=base.with_suffix('.tsv').read_text();tsvs.append(tsv);units.extend(parse_tsv(tsv))
        text='\n'.join(t.rstrip('\n') for t in texts)+'\n' if direction=='horizontal' and blocks_mode and texts else ''.join(texts)
        return {'text':text,'units':units,'tsvs':tsvs,'blocks':boxes}
