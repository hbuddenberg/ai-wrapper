# Stage 1: compile llama-server (PrismML ternary/Bonsai fork) for CC 8.6
# Adds ternary g128 packings (PTQ1_0/PQ2_0) + Hadamard runtime; stock
# llama.cpp refuses PQ2_0/PTQ1_0 and silently corrupts Q2_0. Branch: prism.
FROM nvidia/cuda:12.4.1-devel-ubuntu22.04 AS builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends git cmake build-essential curl libcurl4-openssl-dev \
    && rm -rf /var/lib/apt/lists/*
# CUDA driver stub for the link step: libggml-cuda.so needs libcuda.so.1, which
# is not in the devel image (it's the runtime driver). Expose the stub + path.
RUN ln -sf /usr/local/cuda/lib64/stubs/libcuda.so /usr/local/cuda/lib64/stubs/libcuda.so.1
ENV LIBRARY_PATH=/usr/local/cuda/lib64/stubs
# Pinned to a commit SHA on the fork's 'prism' branch for reproducible builds
# (do NOT build the stale prism-v6 line). Override via --build-arg SOURCE_REF.
ARG SOURCE_REF=25092e7d5d39d22556c97310461be075b5261449
COPY ui.stub.h /tmp/ui.stub.h
WORKDIR /src
RUN git init -q . \
    && git remote add origin https://github.com/PrismML-Eng/llama.cpp.git \
    && git fetch --depth 1 origin "$SOURCE_REF" \
    && git checkout -q FETCH_HEAD \
    && git submodule update --init --recursive --depth 1 \
    && sed -i 's/ llama-ui / /' tools/server/CMakeLists.txt \
    && cp /tmp/ui.stub.h tools/server/ui.h
# -DGGML_NATIVE=OFF: do NOT auto-detect the build runner's CPU instructions.
# CI runners have AVX512 but the target NUC (i3-8109U, Coffee Lake) does NOT ->
# an AVX512-enabled binary SIGILLs on load. Portable baseline + runtime dispatch.
# API-only: drop the embedded Web UI (avoids the non-deterministic HuggingFace
# UI-asset download at build time).
RUN cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=86 -DGGML_NATIVE=OFF -DLLAMA_BUILD_UI=OFF \
    -DCMAKE_EXE_LINKER_FLAGS=-Wl,-rpath-link,/usr/local/cuda/lib64/stubs \
    && cmake --build build --config Release --target llama-server -j"$(nproc)"

# Stage 2: minimal runtime
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04
RUN apt-get update \
    && apt-get install -y --no-install-recommends libcurl4 curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /src/build/bin/llama-server /usr/local/bin/llama-server
COPY --from=builder /src/build/bin/*.so* /usr/local/lib/
ENV LD_LIBRARY_PATH=/usr/local/lib
# No build-time smoke check: llama-server needs libcuda.so.1 (driver), only
# present at runtime via CDI (--device nvidia.com/gpu=all), not at build time.
EXPOSE 8080
ENTRYPOINT []
CMD ["llama-server", "--host", "0.0.0.0", "--port", "8080"]
