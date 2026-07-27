# Stage 1: compile llama-server (UPSTREAM ggerganov/llama.cpp) for CC 8.6.
# The "general purpose" CUDA engine (no fork features). API-only (no web UI).
FROM nvidia/cuda:12.4.1-devel-ubuntu22.04 AS builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends git cmake build-essential curl libcurl4-openssl-dev python3 \
    && rm -rf /var/lib/apt/lists/*
# CUDA driver stub for the link step (libggml-cuda.so needs libcuda.so.1).
RUN ln -sf /usr/local/cuda/lib64/stubs/libcuda.so /usr/local/cuda/lib64/stubs/libcuda.so.1
ENV LIBRARY_PATH=/usr/local/cuda/lib64/stubs
# Pinned to a commit SHA for reproducible/deterministic builds (default branch:
# master). Override via --build-arg SOURCE_REF=<ref> or the ENGINE_SOURCE_REF var.
ARG SOURCE_REF=da296d6e726654949bce10d7cae9a2195d292bfc
COPY ui.stub.h /tmp/ui.stub.h
COPY extract_flags.py /tmp/extract_flags.py
WORKDIR /src
RUN git init -q . \
    && git remote add origin https://github.com/ggerganov/llama.cpp.git \
    && git fetch --depth 1 origin "$SOURCE_REF" \
    && git checkout -q FETCH_HEAD \
    && git submodule update --init --recursive --depth 1 \
    && sed -i 's/ llama-ui / /' tools/server/CMakeLists.txt \
    && cp /tmp/ui.stub.h tools/server/ui.h
# API-only: -DLLAMA_BUILD_UI=OFF source guard + sed above drops 'llama-ui' link.
# -DGGML_NATIVE=OFF: portable baseline (CI runners have AVX512, NUC i3-8109U doesn't).
RUN cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=86 -DGGML_NATIVE=OFF -DLLAMA_BUILD_UI=OFF \
    -DCMAKE_EXE_LINKER_FLAGS=-Wl,-rpath-link,/usr/local/cuda/lib64/stubs \
    && cmake --build build --config Release --target llama-server -j"$(nproc)" \
    && LD_LIBRARY_PATH=/usr/local/cuda/lib64/stubs /src/build/bin/llama-server --help 2>/dev/null | python3 /tmp/extract_flags.py /etc/llama-engine/flags.toml

# Stage 2: minimal runtime
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04
RUN apt-get update \
    && apt-get install -y --no-install-recommends libcurl4 curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /src/build/bin/llama-server /usr/local/bin/llama-server
COPY --from=builder /src/build/bin/*.so* /usr/local/lib/
COPY --from=builder /etc/llama-engine/flags.toml /etc/llama-engine/flags.toml
ENV LD_LIBRARY_PATH=/usr/local/lib
# Build-time flag schema extraction uses libcuda.so stub to run llama-server --help without GPU.
EXPOSE 8080
ENTRYPOINT []
CMD ["llama-server", "--host", "0.0.0.0", "--port", "8080"]
