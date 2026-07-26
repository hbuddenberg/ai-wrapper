// Stub for tools/server/ui.h — makes llama-server build API-ONLY (no embedded Web UI),
// deterministically, WITHOUT the HuggingFace UI-asset download. All UI accessors
// return empty/no-op, so server-http.cpp compiles and no web assets are served.
// The OpenAI-compatible /v1/* API is unaffected.
#pragma once

#include <array>
#include <cstddef>
#include <string>

struct llama_ui_asset {
    std::string           name;
    const unsigned char * data;
    std::size_t           size;
    std::string           etag;
    std::string           type;
};

inline const llama_ui_asset * llama_ui_find_asset(const std::string &) { return nullptr; }
inline bool                     llama_ui_use_gzip()                       { return false; }
inline const std::array<llama_ui_asset, 71> & llama_ui_get_assets() {
    static const std::array<llama_ui_asset, 71> empty{};
    return empty;
}
