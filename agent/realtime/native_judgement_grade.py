"""读取固定判定区域的 GREAT/PERFECT 字形，不产生输入或几何凭据。"""

import hashlib
import json
from pathlib import Path
import time

import cv2
import numpy as np

from .vision_io import imread_unicode

ASSETS = Path(__file__).resolve().parents[2] / 'resource/image/NativeOrdinaryProof'
ROI = (515, 475, 765, 555)
SCALES = tuple(round(.60 + .025 * i, 3) for i in range(11))
TEMPLATE_SHA256 = {
    'PERFECT': 'b5603c8838067de679e63b0728054023aee1bb1aef0696f4cd09db718443e096',
    'GREAT': 'fbaf17bfcbb2111050fec1a38448a7ef99fb67a49196d4e0cb0a193c20d2fb46',
}


def feature(image):
    value=image.astype(np.float32)
    low=value.min(axis=2)
    span=value.max(axis=2)-low
    # 复用色度，投影为P/G共同的品红字形笔画；白glow/橙SLOW不提供正笔画。
    return np.where(span>=40,np.maximum(0,np.minimum(value[:,:,0],value[:,:,2])-value[:,:,1])/np.maximum(span,1),0)


class OrdinaryVisualExtractor:
    def __init__(self, asset_root=None):
        self.templates={}
        root=ASSETS if asset_root is None else Path(asset_root)
        metadata=json.loads((root/'source-and-rects.json').read_text(encoding='utf-8'))
        if metadata.get('bundle_sha256') != 'eaa5bb5aa21e2caefa31ec76476cb825b14617b81f3176cc56467c46a377b475':
            raise ValueError('ordinary-grade-source-mismatch')
        for name in ('PERFECT','GREAT'):
            entry=next(row for row in metadata['sprites'] if row['name']=='judge_'+name.lower())
            path=root/(entry['name']+'.png')
            if (entry['png_sha256'] != TEMPLATE_SHA256[name]
                    or hashlib.sha256(path.read_bytes()).hexdigest() != TEMPLATE_SHA256[name]):
                raise ValueError('ordinary-grade-sha-mismatch')
            image=imread_unicode(path,cv2.IMREAD_UNCHANGED)
            count,labels,stats,_=cv2.connectedComponentsWithStats((image[:,:,3]>=160).astype(np.uint8))
            keep=np.zeros(image.shape[:2],np.uint8)
            for label in range(1,count):
                if stats[label,cv2.CC_STAT_AREA]>=30:keep[labels==label]=255
            contours,_=cv2.findContours(keep,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
            support=np.zeros_like(keep)
            cv2.drawContours(support,contours,-1,255,cv2.FILLED)
            support=cv2.dilate(support,np.ones((3,3),np.uint8))
            ink=feature(image[:,:,:3]);ink[support==0]=0
            full_strokes=((image[:,:,:3].max(axis=2).astype(np.int16)-image[:,:,:3].min(axis=2))>=40).astype(np.float32)
            full_strokes[support==0]=0
            self.templates[name]=[]
            for scale in SCALES:
                template=cv2.resize(ink,None,fx=scale,fy=scale,interpolation=cv2.INTER_LINEAR)
                mask=cv2.resize(support,(template.shape[1],template.shape[0]),interpolation=cv2.INTER_NEAREST)
                strokes=cv2.resize(full_strokes,(template.shape[1],template.shape[0]),interpolation=cv2.INTER_LINEAR)
                self.templates[name].append((scale,template,mask,strokes))

    def scores(self,image):
        started=time.perf_counter()
        if image is None or image.shape!=(720,1280,3):return dict(reason='resolution',category='unknown')
        roi=feature(image[475:555,515:765])
        original_roi=image[475:555,515:765]
        all_ink=(original_roi.max(axis=2).astype(np.int16)-original_roi.min(axis=2))>=40
        output=[]
        for name,variants in self.templates.items():
            best=dict(name=name,score=-1.)
            for scale,template,mask,strokes in variants:
                h,w=template.shape
                # 只在原固定显示中心(640,514)±3px搜索，不把全ROI或动画外形任意拖来匹配。
                x=int(round(640-w/2))-515;y=int(round(514-h/2))-475
                patch=roi[y-3:y+h+3,x-3:x+w+3]
                if patch.shape!=(h+6,w+6):continue
                values=cv2.matchTemplate(patch,template,cv2.TM_CCOEFF_NORMED)
                _,score,_,pos=cv2.minMaxLoc(values)
                if score>best['score']:
                    px=x-3+pos[0];py=y-3+pos[1]
                    target=all_ink[py:py+h,px:px+w]
                    # 独有品红笔画按归一字宽分三区。缺字/只露首字不能只凭一个局部高分通过。
                    coverage=[]
                    for region in np.array_split(np.arange(w),3):
                        positive=(strokes[:,region]>.50)&(mask[:,region]>0)
                        coverage.append(float(target[:,region][positive].mean()) if positive.any() else None)
                    best=dict(name=name,score=float(score),scale=scale,bbox=[px+515,py+475,px+w+515,py+h+475],
                              stroke_coverage=coverage)
            output.append(best)
        output.sort(key=lambda row:row['score'],reverse=True)
        margin=output[0]['score']-output[1]['score']
        coverage=output[0].get('stroke_coverage',[])
        accepted=(output[0]['score']>=.60 and margin>=.25 and len(coverage)==3
                  and all(value is not None and value>=.70 for value in coverage))
        return dict(candidate=output[0]['name'],category=output[0]['name'] if accepted else 'unknown',
                    scores=output,margin=margin,reason='glyph-candidate' if accepted else 'score-margin-or-missing-strokes',
                    elapsed_ms=(time.perf_counter()-started)*1000)
