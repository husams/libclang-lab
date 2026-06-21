// pool_widget.hpp -- PR1 Layer-0 extraction fixture (cidx entity_edge).
//
// Exercises every construction/destruction form that PR1 must capture as
// distinct Layer-0 edge_kind ids (10-16), all inside class methods so PR2
// can roll them up from the enclosing method's owner record.
//
// Widget: a minimal record with default/value/copy/move ctors and a dtor.
// Pool:   a manager record whose methods exercise all construction forms.
//
// Nothing here depends on <memory> — make_unique is tested in pool_widget.cpp
// which can #include <memory>.

#pragma once

namespace graphlab {

struct Widget {
    int value;

    Widget() = default;
    explicit Widget(int v) : value(v) {}
    Widget(const Widget &) = default;
    Widget(Widget &&) = default;
    ~Widget() = default;
};

struct Pool {
    // construct-value (kind 10): Widget w(x) stored in a named variable.
    void make_value(int x) {
        Widget w(x);
        (void)w;
    }

    // construct-temp (kind 11): Widget{} / Widget(x) as a temporary expression.
    void make_temp(int x) {
        (void)Widget(x);
    }

    // construct-copy (kind 13): copy-constructor invocation.
    void make_copy(const Widget &src) {
        Widget c(src);
        (void)c;
    }

    // construct-move (kind 14): move-constructor invocation.
    void make_move(Widget src) {
        Widget m(static_cast<Widget &&>(src));
        (void)m;
    }

    // construct-heap (kind 12): `new Widget(x)`.
    // destroy    (kind 16): `delete p`.
    void make_and_destroy(int x) {
        Widget *p = new Widget(x);
        delete p;
    }
};

// nests (kind 10): a record type declared inside another record. Outer also
// composes Inner (kind 4) via the `inner` value field.
struct Outer {
    struct Inner {
        int depth;
    };
    Inner inner;
};

// befriends (kind 11): a record granting friendship to another record.
// `Vault` declares `Pool` a friend -> befriends(Vault -> Pool).
class Vault {
    friend class Pool;
    int secret;
};

} // namespace graphlab
