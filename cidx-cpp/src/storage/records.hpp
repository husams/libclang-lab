// Plain row structs mirroring the Python dataclasses (design §5.1).
// compile_options is the decoded JSON array (util/json_min); id fields hold
// the SQLite rowid once a row has been read back.
#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <vector>

namespace cidx {

struct Component {
  int64_t id = -1;
  std::string name;
  std::string path; // base path (no version segment)
  std::string kind; // 'repo' | 'external'
  std::optional<std::string> version; // v14: nullable; NULL = unversioned
};

// v14: label registry row
struct Label {
  int64_t id = -1;
  std::string name; // label key, e.g. 'libfoo-include'
  std::string path; // stored verbatim; may contain $VAR
};

struct Directory {
  int64_t id = -1;
  int64_t component_id = -1;
  std::string path; // relative to component.path; '' = root
};

struct File {
  int64_t id = -1;
  int64_t directory_id = -1;
  std::string name;
  std::optional<double> mtime;
  std::optional<std::string> md5;
  std::optional<std::vector<std::string>> compile_options; // decoded JSON
  std::optional<std::string> driver;
  bool indexed = false;
  std::optional<std::string> indexed_at;
  bool args_overridden = false; // flags hand-edited via `cidx file`
};

struct Symbol {
  std::string usr;
  std::string spelling;
  std::string kind; // one of the 17 kSymbolKinds
  std::optional<std::string> qual_name;
  std::optional<std::string> display_name;
  std::optional<std::string> type_info;
  std::optional<int64_t> file_id;
  std::optional<int64_t> line;
  std::optional<int64_t> col;
  std::optional<int64_t> decl_file_id;
  std::optional<int64_t> decl_line;
  std::optional<int64_t> decl_col;
  std::optional<std::string> decl_path; // raw decl path for an unregistered
                                        // (system/stdlib) target -- see schema
  bool is_definition = false;
  bool is_pure = false;
  bool is_static = false; // v12: C++ static member function. Free functions and
                          // non-methods are false; a file-scope `static` free
                          // function is reflected by linkage='internal'.
  bool is_instantiation = false; // v13: implicit template-instantiation node
                                 // (X<int> type node or X<int>::member); its
                                 // definition is expressed via instantiates edge.
  std::optional<std::string> linkage;
  std::optional<std::string> access;
  std::optional<std::string> parent_usr;
  bool resolved = false;
  int64_t id = -1;
};

// -- v7 graph layer records ---------------------------------------------------

struct Edge {
  int64_t src_id = -1;
  int64_t dst_id = -1;
  int64_t kind = 0;                       // edge_kind.id
  int64_t count = 1;
  std::optional<int64_t> base_access;     // inherits
  std::optional<int64_t> is_virtual;      // inherits (0/1)
  std::optional<int64_t> vtable_slot;     // overrides (reserved)
  int64_t id = -1;
};

struct EdgeSite {
  int64_t edge_id = -1;
  std::optional<int64_t> file_id;
  std::optional<int64_t> line;
  std::optional<int64_t> col;
  int64_t conditional = 0;
  std::optional<std::string> args_sig;
  // Phase 2: receiver provenance for virtual dispatch
  std::optional<std::string> recv_src_kind;
  std::optional<std::string> recv_type_usr;
  std::optional<std::string> recv_decl_usr;
  std::optional<int64_t> recv_param_pos;  // 0-based index of receiver in callee params
  std::optional<int64_t> recv_type_is_value;  // v11: receiver held by value (1) else 0/NULL
};

struct CallArg {
  int64_t edge_id = -1;
  int64_t file_id = -1;
  int64_t line = 0;
  int64_t col = 0;
  int64_t position = 0;
  std::string src_kind;             // local|construct|member|global|call_result|unknown
  std::optional<std::string> type_usr;
  std::optional<std::string> decl_usr;
  std::optional<std::string> callee_usr;
  std::optional<int64_t> type_is_value;  // v11: arg held by value (1) else 0/NULL
};

struct TemplateParam {
  int64_t owner_id = -1;
  int64_t position = 0;
  int64_t param_kind = 0;
  std::optional<std::string> name;
  std::optional<std::string> default_txt;
};

struct TemplateArg {
  int64_t owner_id = -1;
  int64_t position = 0;
  int64_t arg_kind = 0;
  std::optional<int64_t> ref_id;
  std::optional<std::string> literal;
};

struct Stats {
  int64_t components = 0;
  int64_t directories = 0;
  int64_t files = 0;
  int64_t files_indexed = 0;
  int64_t symbols = 0;
  int64_t symbols_unresolved = 0;
  std::map<std::string, int64_t> symbols_by_kind;
  int64_t edges = 0;
  std::map<std::string, int64_t> edges_by_kind;
};

} // namespace cidx
