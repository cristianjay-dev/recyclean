# dashboard/utils/utils_images.py
from PIL import Image, ImageOps
from io import BytesIO
from django.core.files.base import ContentFile

def shrink_image_upload(django_file, max_side=1600, webp_quality=82, to_format="WEBP"):
    """
    - Converts to RGB
    - Strips EXIF
    - Resizes so longest side <= max_side
    - Saves as WEBP (or JPEG) with good quality
    Returns (filename, ContentFile)
    """
    im = Image.open(django_file)
    im = ImageOps.exif_transpose(im)          # respect orientation; drops EXIF when saving
    im = im.convert("RGB")

    w, h = im.size
    if max(w, h) > max_side:
        if w >= h:
            new_w = max_side
            new_h = int(h * (max_side / float(w)))
        else:
            new_h = max_side
            new_w = int(w * (max_side / float(h)))
        im = im.resize((new_w, new_h), Image.LANCZOS)

    buf = BytesIO()
    ext = to_format.lower()
    if to_format.upper() == "WEBP":
        im.save(buf, format="WEBP", quality=webp_quality, method=6)
        new_name = (getattr(django_file, "name", "upload") or "upload").rsplit(".", 1)[0] + ".webp"
    else:
        im.save(buf, format="JPEG", quality=85, optimize=True, progressive=True)
        new_name = (getattr(django_file, "name", "upload") or "upload").rsplit(".", 1)[0] + ".jpg"

    buf.seek(0)
    return new_name, ContentFile(buf.read())
