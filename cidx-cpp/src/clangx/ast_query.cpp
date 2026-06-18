// ast_query.cpp — AST walkers and emitters for `cidx ast` (M5).
// Byte-parity port of project/indexer/astcmd.py (walker + emit sections).
// Command handlers (cmd_ast_*) are in commands.cpp.
#include "clangx/ast_query.hpp"

#include <sys/stat.h>

#include <optional>
#include <string>
#include <vector>

#include "clang-c/Index.h"

#include "astcache/astcache.hpp"
#include "cli/args.hpp"
#include "cli/commands.hpp"
#include "cli/json_out.hpp"
#include "cli/kind_names.hpp"
#include "storage/storage.hpp"
#include "util/pathutil.hpp"

namespace cidx {

// ---------------------------------------------------------------------------
// CursorKind classification sets
// ---------------------------------------------------------------------------

bool is_function_kind(CXCursorKind k) {
  // _FUNCTION_KINDS from clang/ast.py lines 122-128.
  switch (k) {
  case CXCursor_FunctionDecl:
  case CXCursor_CXXMethod:
  case CXCursor_Constructor:
  case CXCursor_Destructor:
  case CXCursor_FunctionTemplate:
  case CXCursor_ConversionFunction:
    return true;
  default:
    return false;
  }
}

bool is_cond_kind(CXCursorKind k) {
  // _COND_KINDS from clang/ast.py lines 381-392.
  switch (k) {
  case CXCursor_IfStmt:
  case CXCursor_WhileStmt:
  case CXCursor_DoStmt:
  case CXCursor_ForStmt:
  case CXCursor_SwitchStmt:
  case CXCursor_CaseStmt:
  case CXCursor_ConditionalOperator:
    return true;
  default:
    return false;
  }
}

// ---------------------------------------------------------------------------
// AST walkers
// ---------------------------------------------------------------------------

// Visitor state for for_file_cursors.
struct FileCursorsState {
  const std::string *filename;
  const std::function<void(CXCursor)> *fn;
};

static CXChildVisitResult file_cursors_visitor(CXCursor cursor, CXCursor /*parent*/,
                                               CXClientData data) {
  const auto *st = static_cast<const FileCursorsState *>(data);
  CXSourceLocation loc = clang_getCursorLocation(cursor);
  CXFile file = nullptr;
  unsigned line = 0, col = 0, offset = 0;
  clang_getExpansionLocation(loc, &file, &line, &col, &offset);
  if (file == nullptr) {
    return CXChildVisit_Continue;
  }
  CXString fname = clang_getFileName(file);
  const char *fname_str = clang_getCString(fname);
  const bool in_file =
      (fname_str != nullptr && *st->filename == fname_str);
  clang_disposeString(fname);
  if (!in_file) {
    return CXChildVisit_Continue;
  }
  // Call the visitor.
  (*st->fn)(cursor);
  // Mirror Python _file_cursors: do NOT recurse into function bodies.
  // Function-like kinds get Continue (not Recurse); other top-level
  // entities do not recurse — _file_cursors yields only the top level.
  return CXChildVisit_Continue;
}

void for_file_cursors(CXTranslationUnit tu, const std::string &filename,
                      const std::function<void(CXCursor)> &fn) {
  FileCursorsState st{&filename, &fn};
  CXCursor root = clang_getTranslationUnitCursor(tu);
  clang_visitChildren(root, file_cursors_visitor, &st);
}

// Recursive C++ helper for subtree (no separate visitor — pure C++ recursion).
static void subtree_rec(CXCursor cursor, int depth, CXCursor parent,
                        std::vector<SubtreeNode> &out) {
  // Visit children of cursor.
  struct VisitState {
    int depth;
    CXCursor parent;
    std::vector<SubtreeNode> *out;
  };
  VisitState vs{depth + 1, cursor, &out};
  clang_visitChildren(
      cursor,
      [](CXCursor c, CXCursor p, CXClientData d) -> CXChildVisitResult {
        auto *s = static_cast<VisitState *>(d);
        s->out->push_back({c, s->depth, p});
        subtree_rec(c, s->depth, p, *s->out);
        return CXChildVisit_Continue;
      },
      &vs);
}

std::vector<SubtreeNode> subtree(CXCursor root) {
  std::vector<SubtreeNode> out;
  subtree_rec(root, -1, root, out);
  return out;
}

// ---------------------------------------------------------------------------
// Source location helper
// ---------------------------------------------------------------------------

std::string cursor_loc(CXCursor c) {
  CXSourceLocation loc = clang_getCursorLocation(c);
  CXFile file = nullptr;
  unsigned line = 0, col = 0, offset = 0;
  clang_getExpansionLocation(loc, &file, &line, &col, &offset);
  if (file == nullptr) {
    return "<no-location>";
  }
  CXString fname = clang_getFileName(file);
  const char *raw = clang_getCString(fname);
  std::string base;
  if (raw && *raw) {
    base = pathutil::basename(raw);
  }
  clang_disposeString(fname);
  return base + ":" + std::to_string(line) + ":" + std::to_string(col);
}

// ---------------------------------------------------------------------------
// Extent dict
// ---------------------------------------------------------------------------

json_out::Value extent_dict(CXCursor c) {
  CXSourceRange ext = clang_getCursorExtent(c);
  CXSourceLocation start_loc = clang_getRangeStart(ext);
  CXSourceLocation end_loc = clang_getRangeEnd(ext);

  CXFile sf = nullptr;
  unsigned sl = 0, sc = 0, soff = 0;
  clang_getExpansionLocation(start_loc, &sf, &sl, &sc, &soff);

  unsigned el = 0, ec = 0, eoff = 0;
  clang_getExpansionLocation(end_loc, nullptr, &el, &ec, &eoff);

  json_out::Object obj;
  // "file"
  if (sf) {
    CXString fname = clang_getFileName(sf);
    const char *raw = clang_getCString(fname);
    obj.push_back({"file", raw && *raw
                                ? json_out::Value::of(pathutil::basename(raw))
                                : json_out::Value::null()});
    clang_disposeString(fname);
  } else {
    obj.push_back({"file", json_out::Value::null()});
  }
  // "start": [line, col]
  json_out::Array start_arr;
  start_arr.push_back(json_out::Value::of(static_cast<long long>(sl)));
  start_arr.push_back(json_out::Value::of(static_cast<long long>(sc)));
  obj.push_back({"start", json_out::Value::arr(std::move(start_arr))});
  // "end": [line, col]
  json_out::Array end_arr;
  end_arr.push_back(json_out::Value::of(static_cast<long long>(el)));
  end_arr.push_back(json_out::Value::of(static_cast<long long>(ec)));
  obj.push_back({"end", json_out::Value::arr(std::move(end_arr))});

  return json_out::Value::obj(std::move(obj));
}

// ---------------------------------------------------------------------------
// JSON / text emitters
// ---------------------------------------------------------------------------

json_out::Value cursor_json(CXCursor c, int depth,
                            std::optional<int> max_depth, bool want_tokens,
                            bool want_types) {
  json_out::Object obj;

  // "kind"
  const unsigned kind_val = static_cast<unsigned>(clang_getCursorKind(c));
  obj.push_back({"kind", json_out::Value::of(std::string(cli::kind_name(kind_val)))});

  // "spelling"
  {
    CXString sp = clang_getCursorSpelling(c);
    const char *s = clang_getCString(sp);
    if (s && *s) {
      obj.push_back({"spelling", json_out::Value::of(std::string(s))});
    } else {
      obj.push_back({"spelling", json_out::Value::null()});
    }
    clang_disposeString(sp);
  }

  // "usr"
  {
    CXString usr = clang_getCursorUSR(c);
    const char *u = clang_getCString(usr);
    if (u && *u) {
      obj.push_back({"usr", json_out::Value::of(std::string(u))});
    } else {
      obj.push_back({"usr", json_out::Value::null()});
    }
    clang_disposeString(usr);
  }

  // "extent"
  obj.push_back({"extent", extent_dict(c)});

  // "type" (optional)
  if (want_types) {
    CXType t = clang_getCursorType(c);
    if (t.kind != CXType_Invalid) {
      CXString tsp = clang_getTypeSpelling(t);
      const char *ts = clang_getCString(tsp);
      if (ts && *ts) {
        obj.push_back({"type", json_out::Value::of(std::string(ts))});
      } else {
        obj.push_back({"type", json_out::Value::null()});
      }
      clang_disposeString(tsp);
    } else {
      obj.push_back({"type", json_out::Value::null()});
    }
  }

  // "tokens" (optional)
  if (want_tokens) {
    CXTranslationUnit tu = clang_Cursor_getTranslationUnit(c);
    CXSourceRange extent = clang_getCursorExtent(c);
    CXToken *tokens = nullptr;
    unsigned ntok = 0;
    clang_tokenize(tu, extent, &tokens, &ntok);
    json_out::Array toks;
    for (unsigned i = 0; i < ntok; ++i) {
      CXString ts = clang_getTokenSpelling(tu, tokens[i]);
      const char *raw = clang_getCString(ts);
      toks.push_back(json_out::Value::of(raw ? std::string(raw) : std::string()));
      clang_disposeString(ts);
    }
    if (tokens) {
      clang_disposeTokens(tu, tokens, ntok);
    }
    obj.push_back({"tokens", json_out::Value::arr(std::move(toks))});
  }

  // "children" (recursive, unless max_depth reached)
  if (!max_depth.has_value() || depth < *max_depth) {
    json_out::Array kids;
    struct ChildVisitState {
      int depth;
      std::optional<int> max_depth;
      bool want_tokens;
      bool want_types;
      json_out::Array *kids;
    };
    ChildVisitState cvs{depth, max_depth, want_tokens, want_types, &kids};
    clang_visitChildren(
        c,
        [](CXCursor child, CXCursor /*parent*/, CXClientData d) {
          auto *s = static_cast<ChildVisitState *>(d);
          s->kids->push_back(cursor_json(child, s->depth + 1, s->max_depth,
                                         s->want_tokens, s->want_types));
          return CXChildVisit_Continue;
        },
        &cvs);
    if (!kids.empty()) {
      obj.push_back({"children", json_out::Value::arr(std::move(kids))});
    }
  }

  return json_out::Value::obj(std::move(obj));
}

void dump_text(std::ostream &out, CXCursor c, int depth,
               std::optional<int> max_depth, bool want_tokens,
               bool want_types) {
  std::string indent(static_cast<std::size_t>(depth * 2), ' ');
  const unsigned kind_val = static_cast<unsigned>(clang_getCursorKind(c));
  const char *kname = cli::kind_name(kind_val);

  CXString sp = clang_getCursorSpelling(c);
  const char *name = clang_getCString(sp);
  std::string name_str = (name && *name) ? std::string(name) : "<anon>";
  clang_disposeString(sp);

  std::string typ_str;
  if (want_types) {
    CXType t = clang_getCursorType(c);
    if (t.kind != CXType_Invalid) {
      CXString tsp = clang_getTypeSpelling(t);
      const char *ts = clang_getCString(tsp);
      if (ts && *ts) {
        typ_str = std::string(" : ") + ts;
      }
      clang_disposeString(tsp);
    }
  }

  // Mirror Python: f"{indent}{c.kind.name:<26} {name}{typ}  @ {_loc(c)}"
  // kind_name padded to 26 chars (left-justified).
  std::string kname_padded(kname);
  if (kname_padded.size() < 26) {
    kname_padded += std::string(26 - kname_padded.size(), ' ');
  }
  out << indent << kname_padded << " " << name_str << typ_str
      << "  @ " << cursor_loc(c) << "\n";

  if (want_tokens) {
    CXTranslationUnit tu = clang_Cursor_getTranslationUnit(c);
    CXSourceRange extent = clang_getCursorExtent(c);
    CXToken *tokens = nullptr;
    unsigned ntok = 0;
    clang_tokenize(tu, extent, &tokens, &ntok);
    std::string tok_str;
    for (unsigned i = 0; i < ntok; ++i) {
      if (!tok_str.empty()) {
        tok_str += ' ';
      }
      CXString ts = clang_getTokenSpelling(tu, tokens[i]);
      const char *raw = clang_getCString(ts);
      if (raw) {
        tok_str += raw;
      }
      clang_disposeString(ts);
    }
    if (tokens) {
      clang_disposeTokens(tu, tokens, ntok);
    }
    if (!tok_str.empty()) {
      out << indent << "  ` " << tok_str << "\n";
    }
  }

  if (!max_depth.has_value() || depth < *max_depth) {
    struct ChildState {
      std::ostream *out;
      int depth;
      std::optional<int> max_depth;
      bool want_tokens;
      bool want_types;
    };
    ChildState cs{&out, depth + 1, max_depth, want_tokens, want_types};
    clang_visitChildren(
        c,
        [](CXCursor child, CXCursor /*parent*/, CXClientData d) {
          auto *s = static_cast<ChildState *>(d);
          dump_text(*s->out, child, s->depth, s->max_depth, s->want_tokens,
                    s->want_types);
          return CXChildVisit_Continue;
        },
        &cs);
  }
}

// ---------------------------------------------------------------------------
// Target resolution (mirrors astcmd.resolve_target)
// ---------------------------------------------------------------------------

// Internal helper: find symbol from --usr / --id / --name against the DB.
static std::pair<std::optional<Symbol>, int>
resolve_symbol_from_db(const cli::ParsedArgs &args, Storage &db) {
  if (args.ast_usr) {
    auto s = db.lookup_symbol(*args.ast_usr);
    if (!s) {
      return {std::nullopt, 1}; // caller prints error
    }
    return {*s, 0};
  }
  if (args.ast_id) {
    auto s = db.lookup_symbol_by_id(*args.ast_id);
    if (!s) {
      return {std::nullopt, 1};
    }
    return {*s, 0};
  }
  // --name fuzzy search
  auto hits = db.search_symbols(*args.name, args.kind);
  if (hits.empty()) {
    return {std::nullopt, 1};
  }
  if (hits.size() > 1 && !args.first) {
    return {std::nullopt, 2}; // ambiguous, caller prints list
  }
  return {hits[0], 0};
}

std::pair<std::optional<AstTarget>, int>
resolve_target(const cli::ParsedArgs &args, cli::Context &ctx) {
  // Adhoc flags are in args.rest (everything after "--").
  std::vector<std::string> adhoc_flags;
  bool seen_sep = false;
  for (const std::string &tok : args.rest) {
    if (!seen_sep && tok == "--") {
      seen_sep = true;
      continue;
    }
    adhoc_flags.push_back(tok);
  }

  const std::optional<std::string> focus_usr =
      args.ast_usr ? args.ast_usr : std::optional<std::string>{};
  const std::optional<std::string> focus_name =
      args.name ? args.name : std::optional<std::string>{};

  // --- file target present ---------------------------------------------------
  if (!args.target.empty()) {
    const std::string &target = args.target;

    if (target.find("://") != std::string::npos) {
      // COMPONENT://PATH form.
      const auto sep = target.find("://");
      const std::string comp_name = target.substr(0, sep);
      const std::string rel = target.substr(sep + 3);

      try {
        Storage db(ctx.index_path);
        auto comp = db.get_component_by_name(comp_name);
        if (!comp) {
          *ctx.err << "error: no component named '" << comp_name << "'\n";
          return {std::nullopt, 1};
        }
        const std::string abs_path =
            pathutil::normpath(pathutil::join(comp->path, rel));
        auto rec = db.get_file(abs_path);
        if (!rec) {
          *ctx.err << "error: not in index database: " << abs_path << "\n";
          return {std::nullopt, 1};
        }
        AstTarget t;
        t.abspath = abs_path;
        t.flags = rec->compile_options ? std::vector<std::string>(
                                             rec->compile_options->begin(),
                                             rec->compile_options->end())
                                       : std::vector<std::string>{};
        t.driver = rec->driver;
        t.focus_usr = focus_usr;
        t.focus_name = focus_name;
        return {std::move(t), 0};
      } catch (const std::exception &e) {
        *ctx.err << "error: " << e.what() << "\n";
        return {std::nullopt, 1};
      }
    }

    // Plain file path.
    const std::string abs_path =
        pathutil::abspath(pathutil::expanduser(target));

    if (!adhoc_flags.empty()) {
      // Explicit ad-hoc flags override the index.
      AstTarget t;
      t.abspath = abs_path;
      t.flags = adhoc_flags;
      t.focus_usr = focus_usr;
      t.focus_name = focus_name;
      return {std::move(t), 0};
    }

    // Try the index first.
    try {
      Storage db(ctx.index_path);
      auto rec = db.get_file(abs_path);
      if (rec) {
        AstTarget t;
        t.abspath = abs_path;
        t.flags = rec->compile_options ? std::vector<std::string>(
                                             rec->compile_options->begin(),
                                             rec->compile_options->end())
                                       : std::vector<std::string>{};
        t.driver = rec->driver;
        t.focus_usr = focus_usr;
        t.focus_name = focus_name;
        return {std::move(t), 0};
      }
    } catch (...) {
      // DB unavailable: fall through to ad-hoc.
    }

    // Not in index and no flags given: warn and parse with defaults.
    {
      struct stat st{};
      if (::stat(abs_path.c_str(), &st) != 0) {
        *ctx.err << "error: no such file and not in index: " << target << "\n";
        return {std::nullopt, 1};
      }
    }
    *ctx.err << "warning: " << abs_path
             << " is not in the index and no flags were given "
                "(pass '-- <flags>'); parsing with defaults\n";
    AstTarget t;
    t.abspath = abs_path;
    t.flags = {};
    t.focus_usr = focus_usr;
    t.focus_name = focus_name;
    return {std::move(t), 0};
  }

  // --- no target: resolve symbol from index ---------------------------------
  const bool has_selector = args.ast_usr || args.ast_id || args.name;
  if (!has_selector) {
    *ctx.err
        << "error: need a symbol selector (--usr/--id/--name) or a "
           "FILE/COMPONENT://PATH target\n";
    return {std::nullopt, 2};
  }

  try {
    Storage db(ctx.index_path);

    // Resolve symbol.
    auto [sym_opt, sym_rc] = resolve_symbol_from_db(args, db);
    if (!sym_opt) {
      // Print appropriate error.
      if (args.ast_usr) {
        *ctx.err << "error: no symbol with USR '" << *args.ast_usr << "'\n";
      } else if (args.ast_id) {
        *ctx.err << "error: no symbol with id " << *args.ast_id << "\n";
      } else {
        // --name: get the hits for the disambiguation message.
        auto hits = db.search_symbols(*args.name, args.kind);
        if (hits.empty()) {
          *ctx.err << "error: no symbol matches --name '" << *args.name << "'"
                   << (args.kind ? " (kind " + *args.kind + ")" : "") << "\n";
        } else {
          *ctx.err << "error: --name '" << *args.name << "' matches "
                   << hits.size()
                   << " symbols; disambiguate with --usr/--id (or pass "
                      "--first):\n";
          const std::size_t show = std::min<std::size_t>(hits.size(), 25);
          for (std::size_t i = 0; i < show; ++i) {
            const auto &s = hits[i];
            const std::string loc = s.qual_name ? *s.qual_name : s.spelling;
            *ctx.err << "  #" << s.id << "  " << s.kind << "  " << loc
                     << "  [" << s.usr << "]\n";
          }
          if (hits.size() > 25) {
            *ctx.err << "  ... and " << (hits.size() - 25) << " more\n";
          }
        }
      }
      return {std::nullopt, sym_rc};
    }
    const Symbol &sym = *sym_opt;

    // Get file id.
    const std::optional<int64_t> file_id =
        sym.file_id ? sym.file_id : sym.decl_file_id;
    if (!file_id) {
      *ctx.err << "error: symbol '" << sym.spelling
               << "' has no indexed file (declaration-only/external)\n";
      return {std::nullopt, 1};
    }

    auto rec = db.get_file_by_id(*file_id);
    auto path = db.file_abs_path(*file_id);
    if (!rec || !path) {
      *ctx.err << "error: cannot resolve the symbol's file in the index\n";
      return {std::nullopt, 1};
    }

    AstTarget t;
    t.abspath = *path;
    t.flags =
        rec->compile_options
            ? std::vector<std::string>(rec->compile_options->begin(),
                                       rec->compile_options->end())
            : std::vector<std::string>{};
    t.driver = rec->driver;
    t.focus_usr = sym.usr;
    return {std::move(t), 0};

  } catch (const std::exception &e) {
    *ctx.err << "error: " << e.what() << "\n";
    return {std::nullopt, 1};
  }
}

} // namespace cidx
