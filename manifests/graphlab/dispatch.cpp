// dispatch.cpp — definitions + call sites for dispatch.hpp.
//
// Exercises, for `cidx graph callers ... --include-overrides`:
//   * execute() -> base::doSomething        (the static calls edge)
//   * child/sibling/grandchild::doSomething override base::doSomething
//   * dispatch_calls: execute -> {child,sibling,grandchild}::doSomething
//   * a DIRECT call to child::doSomething for contrast (direct vs virtual)
#include "dispatch.hpp"

namespace dispatch {

void child::doSomething() {}
void sibling::doSomething() {}
void grandchild::doSomething() {}

// The motivating caller: a concrete child, invoked through the base's
// non-virtual execute(). At run time this reaches child::doSomething, but the
// recorded calls edge is execute -> base::doSomething.
void call() {
  child c;
  c.execute();
}

// Same shape one level deeper — reaches grandchild::doSomething at run time.
void call_grandchild() {
  grandchild g;
  g.execute();
}

// A DIRECT (non-dispatch) call straight to the override. This makes
// child::doSomething have a real incoming `calls` edge, so plain
// callers(child::doSomething) returns `use_child` while `execute` shows up
// only under --include-overrides.
void use_child(child& c) { c.doSomething(); }

}  // namespace dispatch
