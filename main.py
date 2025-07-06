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
    request.state.start_time = start_time
    
    response = await call_next(request)
    
    end_time = time.perf_counter()
    total_time_ms = round((end_time - start_time) * 1000, 2)

    timing_message = f"TOTAL REQ-RES TIMING | method={request.method} | url={request.url.path} | total_request_response_ms={total_time_ms}"
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
        app_logger.error(f"PDF validation failed: {str(e)}")
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_PDF",
                "message": f"File is not a valid PDF or is corrupted: {str(e)}"
            }
        )
    
    page_count = len(doc)
    doc.close()
    
    if page_count != 3:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_PAGE_COUNT",
                "message": f"PDF must have exactly 3 pages, found {page_count} pages"
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

        doc.close()

        img_bytes = []
        for idx, img in enumerate(img_list):
            buf = io.BytesIO()
            img.save(buf, format="JPEG")
            byte_data = buf.getvalue()
            img_bytes.append(byte_data)

        t1 = time.perf_counter()
        result = rekognition_client.compare_faces(
            SourceImage={'Bytes': img_bytes[0]},
            TargetImage={'Bytes': img_bytes[1]},
            SimilarityThreshold=FACE_SIMILARITY_THRESHOLD * 100
        )
        t2 = time.perf_counter()

        if result['FaceMatches']:
            similarity = result['FaceMatches'][0]['Similarity']
            app_logger.info(f"AWS Rekognition face similarity: {similarity:.2f}")
        else:
            similarity = 0.0
            app_logger.warning("No face match found by Rekognition")

        latency_metrics = {
            "image_preprocessing": round((t1 - start_time) * 1000, 2),
            "face_comparison": round((time.perf_counter() - t1) * 1000, 2),
            "total": round((time.perf_counter() - start_time) * 1000, 2)
        }

        app_logger.info(f"Latency (ms): image_preprocessing={latency_metrics['image_preprocessing']}ms, "
                        f"rekognition_face_comparison={latency_metrics['face_comparison']}ms, total={latency_metrics['total']}ms")

        return {
            "similarity": round(similarity, 2),
            "latency_ms": latency_metrics
        }

    except Exception as e:
        app_logger.error(f"Face comparison failed: {e}")
        raise RuntimeError(f"Face comparison failed: {e}")

def compare_texts_from_pdf(pdf_bytes: bytes, rekognition_client=None) -> dict:
    start_time = time.perf_counter()
    if rekognition_client is None:
        rekognition_client = boto3.client("rekognition", region_name="us-east-1")

    latencies = {}
    t0 = time.perf_counter()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    form_text = doc[0].get_text()
    latencies['form_extraction_ms'] = round((time.perf_counter() - t0) * 1000, 2)

    form_patterns = {
        "pan_number": r"PAN NUMBER\s+([A-Z]{5}[0-9]{4}[A-Z])",
        "full_name": r"FULL NAME\s+([A-Z ]+)",
        "father_name": r"FATHER NAME\s+([A-Z ]+)",
        "dob": r"DATE OF BIRTH\s+\(dd/mm/yyyy\)\s+([0-9]{2}/[0-9]{2}/[0-9]{4})"
    }
    form_data = {field: (match.group(1).strip() if (match := re.search(pattern, form_text)) else None)
                 for field, pattern in form_patterns.items()}
    
    print("EXTRACTED FORM DATA (Pattern Matched)")
    for field, value in form_data.items():
        print(f"{field}: {value}")


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
    
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    image_bytes = buf.getvalue()
    doc.close()

    t1 = time.perf_counter()
    response = rekognition_client.detect_text(Image={"Bytes": image_bytes})
    latencies['rekognition_ocr_ms'] = round((time.perf_counter() - t1) * 1000, 2)

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
    
    print("EXTRACTED PAN DATA (From OCR)")
    for field, value in pan_data.items():
        print(f"{field}: {value}")

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

    total_latency = round((time.perf_counter() - start_time) * 1000, 2)
    app_logger.info(f"Latency (ms): form_extraction={latencies['form_extraction_ms']}ms, "
                    f"rekognition_ocr={latencies['rekognition_ocr_ms']}ms, "
                    f"comparison={latencies['comparison_ms']}ms, total={total_latency}ms")

    return {
        "form_data": form_data,
        "pan_data": pan_data,
        "match_scores": comparison,
        "latency_ms": latencies,
        "total": total_latency
    }

async def run_parallel(pdf_bytes: bytes, pdf_path: str):
    rekognition_client = boto3.client("rekognition", region_name="us-east-1")
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=2)

    kyc_future = loop.run_in_executor(executor, compare_texts_from_pdf, pdf_bytes, rekognition_client)
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
async def validate_application(request: Request, file: UploadFile = File(...), application_id: str = None):
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
        parallel_processing_ms = round((end - start) * 1000, 2)
        total_processing_ms = round((time.perf_counter() - request.state.start_time) * 1000, 2)
        app_logger.info(f"Total function processing time: {parallel_processing_ms} ms")

        metrics = {
            "processing_ms": total_processing_ms,
            "ocr_ms": kyc.get("total", 0),
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

    except HTTPException as he:
        request_logger.error(f"Validation error | application_id={application_id} | error={he.detail}")
        raise he
    except Exception as e:
        error_message = str(e) if str(e) else "Unknown processing error occurred"
        request_logger.error(f"ERROR processing | application_id={application_id} | error={error_message}")
        raise HTTPException(status_code=500, detail={"code": "PROCESSING_FAILED", "message": error_message})