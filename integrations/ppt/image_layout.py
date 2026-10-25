"""Fit image into a PPT rectangle, keeping aspect ratio."""
from PIL import Image
from pptx.util import Inches
def place_image(slide,path,x,y,w,h):
    with Image.open(path) as im: iw,ih=im.size
    pw=min(w,h*iw/ih);ph=pw*ih/iw
    return slide.shapes.add_picture(path,Inches(x+(w-pw)/2),Inches(y+(h-ph)/2),
                                    width=Inches(pw),height=Inches(ph))
