// dispatch.hpp — virtual-dispatch caller edges (kind 18 `dispatch_calls`).
//
// The fixture for cidx's `callers(..., include_overrides=True)` /
// `graph callers --include-overrides`. A non-virtual `execute()` calls a
// pure-virtual `doSomething()`, so libclang records the call against the
// DECLARED target `base::doSomething`. Asking who calls a concrete override
// (`child::doSomething`) is empty on the raw graph; `resolve` materialises
// `dispatch_calls` edges (caller -> each transitive override) to recover it.
#ifndef GRAPHLAB_DISPATCH_HPP
#define GRAPHLAB_DISPATCH_HPP

namespace dispatch {

struct base {
  virtual ~base() = default;
  // Pure virtual: no body. The dispatch target every override attaches to.
  virtual void doSomething() = 0;
  // Non-virtual: its call to doSomething() is recorded against base::doSomething.
  void execute() { doSomething(); }
};

// One override. callers(child::doSomething, include_overrides) => execute.
struct child : public base {
  void doSomething() override;
};

// A sibling override — makes execute a *conservative* virtual caller of BOTH
// child::doSomething and sibling::doSomething (no type info to disambiguate).
struct sibling : public base {
  void doSomething() override;
};

// Transitive override two levels down. A call to base::doSomething dispatches
// here too, so callers(grandchild::doSomething, include_overrides) => execute.
struct grandchild : public child {
  void doSomething() override;
};

}  // namespace dispatch

#endif  // GRAPHLAB_DISPATCH_HPP
