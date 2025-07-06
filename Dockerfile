FROM public.ecr.aws/lambda/python:3.10

RUN yum install -y gcc gcc-c++ make zlib-devel libjpeg-turbo-devel cmake git

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY main.py .

CMD ["main.handler"]