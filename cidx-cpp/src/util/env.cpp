#include "util/env.hpp"

#include <cctype>
#include <cstdlib>

namespace cidx {

namespace {

// Python str.strip() + str.lower() equivalent for ASCII env values.
std::string strip_lower(const char *v) {
  std::string s = (v != nullptr) ? v : "";
  const char *ws = " \t\n\r\v\f";
  const auto begin = s.find_first_not_of(ws);
  if (begin == std::string::npos) {
    return "";
  }
  const auto end = s.find_last_not_of(ws);
  s = s.substr(begin, end - begin + 1);
  for (auto &c : s) {
    c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  }
  return s;
}

} // namespace

std::optional<std::string> get_env(const char *name) {
  const char *v = std::getenv(name);
  if (v == nullptr) {
    return std::nullopt;
  }
  return std::string(v);
}

bool env_flag_disabled_gnuc(const char *v) {
  const std::string s = strip_lower(v);
  return s == "0" || s == "off" || s == "none" || s == "false";
}

bool env_flag_false_headers(const char *v) {
  const std::string s = strip_lower(v);
  return s == "0" || s == "false" || s == "no" || s == "off";
}

} // namespace cidx
