// chain.hpp -- a 4-level SINGLE-INHERITANCE CHAIN: A <- B <- C <- D.
//
// Each class derives from the previous one and overrides rank(), so the index
// gets a chain of `inherits` edges (B->A, C->B, D->C) and a parallel chain of
// `overrides` edges. This exercises TRANSITIVE hierarchy queries (D's ancestors
// = {C, B, A}; A's descendants = {B, C, D}) and deep dynamic dispatch.
#ifndef GRAPHLAB_CHAIN_HPP
#define GRAPHLAB_CHAIN_HPP

namespace chain {

struct A {
    virtual ~A() = default;
    virtual int rank() const;   // base of the chain
};

struct B : A {                  // B inherits A
    int rank() const override;
};

struct C : B {                  // C inherits B (-> A transitively)
    int rank() const override;
};

struct D : C {                  // D inherits C (-> B -> A transitively)
    int rank() const override;
};

// Takes the top of the chain by reference; dynamic dispatch reaches whichever
// concrete rank() the run-time type provides.
int top_rank(const A& a);

} // namespace chain

#endif
