// dispatch.cpp — the exact discussed use case for cidx `dispatch_calls`
// (virtual-dispatch caller edges, kind 18).
//
// `execute()` calls the pure-virtual `doSomething()`, so the recorded call edge
// is execute -> base::doSomething. `child` overrides doSomething. On the raw
// graph callers(child::doSomething) is empty; after `resolve` materialises the
// dispatch_calls edge, callers(child::doSomething, include_overrides=True)
// returns `execute` (base's instance dispatches to the child's override).

struct base {
  virtual void doSomething() = 0;
  void execute() {
    doSomething();
  }
};

void print() {
}


struct child : public base {
  void doSomething() override { print(); }
};

// The caller: a concrete `child`, invoked through the base's non-virtual
// execute(). At run time this reaches child::doSomething, but the recorded
// call edge is execute -> base::doSomething.
void call() {
  child c;
  c.execute();
}
