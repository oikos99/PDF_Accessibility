FROM public.ecr.aws/sam/build-python3.12:latest
WORKDIR /tmp/build
COPY requirements.txt .
RUN mkdir -p /asset \
    && python -m pip install --no-cache-dir -r requirements.txt -t /asset
COPY main.py .
RUN python -m py_compile main.py \
    && cp main.py /asset/main.py
