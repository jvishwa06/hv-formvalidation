import io
import re
import fitz
import boto3
import time
import random
import logging
import asyncio
from mangum import Mangum
from PIL import Image
from datetime import datetime, timezone
from nameparser import HumanName
from rapidfuzz import fuzz
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from concurrent.futures import ThreadPoolExecutor
from logging import StreamHandler

MAX_FILE_SIZE_MB = 10
REQUIRED_PAGE_COUNT = 3
TEXT_SIMILARITY_THRESHOLD = 80
FACE_SIMILARITY_THRESHOLD = 0.7

formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
stream_handler = StreamHandler()
stream_handler.setFormatter(formatter)

app_logger = logging.getLogger("application")
app_logger.setLevel(logging.INFO)
app_logger.addHandler(stream_handler)

request_logger = logging.getLogger("requests")
request_logger.setLevel(logging.INFO)
request_logger.addHandler(stream_handler)

app = FastAPI()
handler = Mangum(app)

@app.middleware("http")
async def request_timing_middleware(request: Request, call_next):
    start_time = time.perf_counter()
    
    response = await call_next(request)
    
    end_time = time.perf_counter()
    total_time_ms = round((end_time - start_time) * 1000, 2)
    
    timing_message = f"TOTAL REQ-RES TIMING | method={request.method} | url={request.url.path} | total_time_ms={total_time_ms}"
    request_logger.info(timing_message)
    print(timing_message)
    
    response.headers["X-Request-Time-Ms"] = str(total_time_ms)
    
    return response

def validate_pdf_file(file: UploadFile, contents: bytes) -> None:
    file_size_mb = len(contents) / (1024 * 1024)
    if file_size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "FILE_TOO_LARGE",
                "message": f"File size ({file_size_mb:.2f} MB) exceeds maximum allowed size ({MAX_FILE_SIZE_MB} MB)"
            }
        )
    
    try:
        doc = fitz.open(stream=contents, filetype="pdf")
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_PDF",
                "message": "File is not a valid PDF or is corrupted"
            }
        )
    
    page_count = len(doc)
    doc.close()
    
    if page_count != REQUIRED_PAGE_COUNT:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_PAGE_COUNT",
                "message": f"PDF must have exactly {REQUIRED_PAGE_COUNT} pages, found {page_count} pages"
            }
        )
    
    if file.filename and not file.filename.lower().endswith('.pdf'):
        app_logger.warning(f"File {file.filename} does not have .pdf extension but contains valid PDF content")

def compare_faces_from_pdf(pdf_bytes: bytes, rekognition_client=None, resize_width=600):
    start_time = time.perf_counter()
    if rekognition_client is None:
        rekognition_client = boto3.client("rekognition", region_name="us-east-1")

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        img_list = []

        for page_number in [1, 2]:
            page = doc[page_number]
            images = page.get_images(full=True)
            if not images:
                raise ValueError(f"No image on page {page_number + 1}")
            xref = images[0][0]
            base_image = doc.extract_image(xref)
            img = Image.open(io.BytesIO(base_image["image"]))

            w_percent = resize_width / float(img.size[0])
            h_size = int(img.size[1] * w_percent)
            img = img.resize((resize_width, h_size), Image.LANCZOS)
            img_list.append(img)

        total_width = img_list[0].width + img_list[1].width
        max_height = max(img_list[0].height, img_list[1].height)
        combined = Image.new('RGB', (total_width, max_height), (255, 255, 255))
        combined.paste(img_list[0], (0, 0))
        combined.paste(img_list[1], (img_list[0].width, 0))

        t1 = time.perf_counter()
        buf = io.BytesIO()
        combined.save(buf, format="JPEG")
        combined_bytes = buf.getvalue()

        response = rekognition_client.detect_faces(Image={'Bytes': combined_bytes}, Attributes=['DEFAULT'])
        faces = response.get("FaceDetails", [])
        if len(faces) < 2:
            raise ValueError("Expected 2 faces but found less.")

        app_logger.info(f"Face detection on combined Page 2+3 image took {round((time.perf_counter() - t1) * 1000, 2)} ms")

        w, h = combined.size
        face_crops = []
        for face in faces[:2]:
            box = face["BoundingBox"]
            left = int(box["Left"] * w)
            top = int(box["Top"] * h)
            right = left + int(box["Width"] * w)
            bottom = top + int(box["Height"] * h)
            crop = combined.crop((left, top, right, bottom))
            crop_buf = io.BytesIO()
            crop.save(crop_buf, format="JPEG")
            face_crops.append(crop_buf.getvalue())

        t2 = time.perf_counter()
        result = rekognition_client.compare_faces(SourceImage={'Bytes': face_crops[0]}, TargetImage={'Bytes': face_crops[1]}, SimilarityThreshold=FACE_SIMILARITY_THRESHOLD * 100)
        similarity = result['FaceMatches'][0]['Similarity'] if result['FaceMatches'] else 0.0

        app_logger.info(f"Face comparison took {round((time.perf_counter() - t2) * 1000, 2)} ms")

        return {
            "similarity": round(similarity, 2),
            "latency_ms": {
                "image_preprocessing": round((t1 - start_time) * 1000, 2),
                "face_detection": round((t2 - t1) * 1000, 2),
                "face_comparison": round((time.perf_counter() - t2) * 1000, 2),
                "total": round((time.perf_counter() - start_time) * 1000, 2)
            }
        }

    except Exception as e:
        raise RuntimeError(f"Face comparison failed: {e}")

def process_kyc_pdf(pdf_bytes: bytes, rekognition_client=None) -> dict:
    start_time = time.perf_counter()
    if rekognition_client is None:
        rekognition_client = boto3.client("rekognition", region_name="us-east-1")

    latencies = {}
    t0 = time.perf_counter()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    form_text = doc[0].get_text()
    latencies['form_extraction_ms'] = round((time.perf_counter() - t0) * 1000, 2)
    app_logger.info(f"Page 1 extracted text in {latencies['form_extraction_ms']} ms")

    form_patterns = {
        "pan_number": r"PAN NUMBER\s+([A-Z]{5}[0-9]{4}[A-Z])",
        "full_name": r"FULL NAME\s+([A-Z ]+)",
        "father_name": r"FATHER NAME\s+([A-Z ]+)",
        "dob": r"DATE OF BIRTH\s+\(dd/mm/yyyy\)\s+([0-9]{2}/[0-9]{2}/[0-9]{4})"
    }
    form_data = {field: (match.group(1).strip() if (match := re.search(pattern, form_text)) else None)
                 for field, pattern in form_patterns.items()}

    page_2 = doc[1]
    images = page_2.get_images(full=True)
    if not images:
        doc.close()
        raise ValueError("No image found on page 2")
    
    xref = images[0][0]
    base_image = doc.extract_image(xref)
    img = Image.open(io.BytesIO(base_image["image"]))
    
    resize_width = 600
    w_percent = resize_width / float(img.size[0])
    h_size = int(img.size[1] * w_percent)
    img = img.resize((resize_width, h_size), Image.LANCZOS)
    
    app_logger.info(f"Page 2 image processed and resized (size: {img.size})")
    
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    image_bytes = buf.getvalue()
    doc.close()

    t1 = time.perf_counter()
    response = rekognition_client.detect_text(Image={"Bytes": image_bytes})
    latencies['rekognition_ocr_ms'] = round((time.perf_counter() - t1) * 1000, 2)
    app_logger.info(f"Page 2 OCR completed in {latencies['rekognition_ocr_ms']} ms")

    text_lines = [d["DetectedText"] for d in response["TextDetections"] if d["Type"] == "LINE"]
    full_text = "\n".join(text_lines)

    pan_patterns = {
        "pan_number": r"\b([A-Z]{5}[0-9]{4}[A-Z])\b",
        "full_name": r"Name\s*[:\-]?\s*([A-Z ]{3,})",
        "father_name": r"Father['’]s Name\s*[:\-]?\s*([A-Z ]{3,})",
        "dob": r"(?i)date\s*of\s*birth\s*\n(?:.*\n)*?([0-9]{2}/[0-9]{2}/[0-9]{4})"
    }
    pan_data = {field: (match.group(1).strip() if (match := re.search(pattern, full_text, flags=re.IGNORECASE)) else None)
                for field, pattern in pan_patterns.items()}

    t2 = time.perf_counter()
    def name_score(n1, n2):
        if not n1 or not n2:
            return 0.0
        hn1, hn2 = HumanName(n1.upper().strip()), HumanName(n2.upper().strip())
        return round(fuzz.partial_ratio(f"{hn1.first} {hn1.last}".strip(), f"{hn2.first} {hn2.last}".strip()), 2)

    comparison = {}
    for key in ["full_name", "father_name"]:
        comparison[key] = name_score(form_data.get(key, ""), pan_data.get(key, ""))
    for key in ["pan_number", "dob"]:
        val1, val2 = form_data.get(key, ""), pan_data.get(key, "")
        comparison[key] = 100.0 if val1 and val2 and val1.upper() == val2.upper() else round(fuzz.ratio(val1, val2), 2)

    latencies['comparison_ms'] = round((time.perf_counter() - t2) * 1000, 2)
    app_logger.info(f"Text comparison completed in {latencies['comparison_ms']} ms")

    return {
        "form_data": form_data,
        "pan_data": pan_data,
        "match_scores": comparison,
        "latency_ms": latencies,
        "total": round((time.perf_counter() - start_time) * 1000, 2)
    }

async def run_parallel(pdf_bytes: bytes, pdf_path: str):
    rekognition_client = boto3.client("rekognition", region_name="us-east-1")
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=2)

    kyc_future = loop.run_in_executor(executor, process_kyc_pdf, pdf_bytes, rekognition_client)
    face_future = loop.run_in_executor(executor, compare_faces_from_pdf, pdf_bytes, rekognition_client)
    kyc_result, face_result = await asyncio.gather(kyc_future, face_future)
    return {"kyc_validation": kyc_result, "face_similarity": face_result}

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(),
        "service": "FORM-VALIDATION-API"
    }

@app.post("/v1/validate-application")
async def validate_application(file: UploadFile = File(...), application_id: str = None):
    application_id = application_id or f"APP-{random.randint(10000, 99999)}"
    request_logger.info(f"START request | file={file.filename} | application_id={application_id}")

    try:
        contents = await file.read()
        
        validate_pdf_file(file, contents)

        start = time.perf_counter()
        result = await run_parallel(contents, None)
        end = time.perf_counter()

        kyc, face = result["kyc_validation"], result["face_similarity"]
        field_matches, field_pass, errors = {}, True, []

        for key, score in kyc["match_scores"].items():
            is_pass = score >= TEXT_SIMILARITY_THRESHOLD
            field_matches[key] = {"score": score, "pass": is_pass}
            if not is_pass:
                field_pass = False
                errors.append({
                    "code": f"{key.upper()}_MISMATCH",
                    "message": f"{key.replace('_', ' ').upper()} differs between Page 1 and PAN card"
                })

        face_pass = face["similarity"] >= (FACE_SIMILARITY_THRESHOLD * 100)
        total_processing_ms = round((end - start) * 1000, 2)
        app_logger.info(f"Total processing time: {total_processing_ms} ms")

        metrics = {
            "processing_ms": total_processing_ms,
            "ocr_ms": kyc["latency_ms"].get("rekognition_ocr_ms", 0),
            "face_match_ms": face["latency_ms"].get("total", 0)
        }

        response_data = {
            "application_id": application_id,
            "field_matches": field_matches,
            "field_pass": field_pass,
            "face_match": {"similarity": face["similarity"], "pass": face_pass},
            "overall_pass": field_pass and face_pass,
            "errors": errors,
            "processed_at": datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(),
            "metrics": metrics
        }
        request_logger.info(f"END request | result={response_data}")
        return response_data

    except Exception as e:
        request_logger.error(f"ERROR processing | application_id={application_id} | error={str(e)}")
        raise HTTPException(status_code=500, detail={"code": "PROCESSING_FAILED", "message": str(e)})