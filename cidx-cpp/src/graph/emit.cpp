// graph/emit.cpp -- output emitters for the `graph` command group.
//
// Mirrors cli.py:_emit_edges/_emit_syms (cli.py:1054-1091).
// Byte-identical output to Python for both text and --json modes.
#include "graph/emit.hpp"

#include <string>
#include <vector>

#include "cli/format.hpp"
#include "cli/json_out.hpp"

namespace cidx {
namespace graph {

// ---- emit_edges -----------------------------------------------------------
// cli.py:1054-1069

void emit_edges(GraphQuery &g, const std::vector<Edge> &edges, bool json_mode,
                std::ostream &out, const std::string &header) {
  if (json_mode) {
    // R8: re-query sites via g.sites(e) (A8 limit 200), not eager e.sites
    json_out::Array arr;
    arr.reserve(edges.size());
    for (const Edge &e : edges) {
      auto edge_sites = g.sites(e.edge_id, 200);
      arr.push_back(e.to_dict(edge_sites));
    }
    out << json_out::dumps_indent2(json_out::Value::arr(std::move(arr))) << "\n";
    return;
  }

  // Text output: header, per-edge lines, trailer.
  out << header << "\n";

  // Width = max(len(peer.name or peer.usr)) across all edges.
  std::size_t width = 0;
  for (const Edge &e : edges) {
    const std::string &nm = e.peer.name.empty() ? e.peer.usr : e.peer.name;
    if (nm.size() > width) {
      width = nm.size();
    }
  }

  for (const Edge &e : edges) {
    // count suffix: only when count > 1 (or truthy and != 1)
    std::string cnt_str;
    if (e.count && e.count != 1) {
      cnt_str = "  x" + std::to_string(e.count);
    }
    // sample site: first site from A8 limit=1
    std::string site_str;
    {
      auto sample = g.sites(e.edge_id, 1);
      if (!sample.empty()) {
        site_str = "  (" + sample[0].loc() + ")";
      }
    }
    const std::string stub_str = e.peer.is_stub() ? "  [stub]" : "";
    const std::string &nm = e.peer.name.empty() ? e.peer.usr : e.peer.name;
    out << "  " << cli::format::ljust(e.peer.kind, 14) << " "
        << cli::format::ljust(nm, width) << "  @" << e.peer.loc()
        << cnt_str << site_str << stub_str << "\n";
  }
  out << edges.size() << " result(s)\n";
}

// ---- emit_syms ------------------------------------------------------------
// cli.py:1072-1091

void emit_syms(const std::vector<Sym> &syms, bool json_mode, std::ostream &out,
               const std::string &header,
               const std::unordered_map<int64_t, int> *depths) {
  if (json_mode) {
    json_out::Array arr;
    arr.reserve(syms.size());
    for (const Sym &s : syms) {
      json_out::Value v = s.to_dict();
      if (depths) {
        // Append "depth" key LAST (walk only, R7)
        auto it = depths->find(s.id);
        if (it != depths->end()) {
          v.o.push_back({"depth", json_out::Value::of(it->second)});
        } else {
          v.o.push_back({"depth", json_out::Value::null()});
        }
      }
      arr.push_back(std::move(v));
    }
    out << json_out::dumps_indent2(json_out::Value::arr(std::move(arr))) << "\n";
    return;
  }

  // Text output
  out << header << "\n";

  // Width = max(len(name or usr)) across all syms.
  std::size_t width = 0;
  for (const Sym &s : syms) {
    const std::string &nm = s.name.empty() ? s.usr : s.name;
    if (nm.size() > width) {
      width = nm.size();
    }
  }

  for (const Sym &s : syms) {
    std::string dep_str;
    if (depths) {
      auto it = depths->find(s.id);
      if (it != depths->end()) {
        dep_str = "  d" + std::to_string(it->second);
      }
    }
    const std::string stub_str = s.is_stub() ? "  [stub]" : "";
    const std::string &nm = s.name.empty() ? s.usr : s.name;
    out << "  " << cli::format::ljust(s.kind, 14) << " "
        << cli::format::ljust(nm, width) << "  @" << s.loc()
        << dep_str << stub_str << "\n";
  }
  out << syms.size() << " result(s)\n";
}

} // namespace graph
} // namespace cidx
