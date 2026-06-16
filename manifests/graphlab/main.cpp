// main.cpp -- drives every feature so the indexer sees real USES, not just decls.
#include <string>
#include <vector>

#include "shapes.hpp"
#include "creatures.hpp"
#include "containers.hpp"
#include "pipeline.hpp"
#include "chain.hpp"
#include "nested.hpp"

// A free function template, defined here and used below (defined + used).
template <class T>
T twice(T v) {
    return cont::combine(v, v);   // function template calling another fn template
}

// Explicit instantiation of twice<int> (a construct in its own right). NOTE:
// libclang still does not expose the instantiated body's internal call to
// combine<int> -- see instantiations.cpp for the limitation.
template int twice<int>(int);

int main() {
    // ---- deep call graph + repeated calls (in run_pipeline) ----------------
    double total = app::run_pipeline(1.5);

    // ---- dynamic dispatch through an abstract base -------------------------
    geo::Circle c(2.0);
    geo::Rectangle r(3.0, 4.0);
    total += app::measure(c);     // measure(const Shape&) -> Circle overrides
    total += app::measure(r);     // measure(const Shape&) -> Rectangle overrides

    // virtual dispatch via a base-pointer container
    std::vector<geo::Shape*> shapes{&c, &r};
    for (geo::Shape* s : shapes)
        total += s->area();       // dynamic dispatch in a loop

    // ---- function overloads: each call resolves to a distinct overload -----
    int sa = app::scale(3);            // -> scale(int)
    double sb = app::scale(3.5);       // -> scale(double)
    int sc = app::scale(3, 4);         // -> scale(int, int)
    total += sa + sb + sc;

    // ---- heap allocation: new (via make_shape) then delete (via consume) ---
    total += app::consume(app::make_shape(1.25));   // new Circle ... delete

    // ---- inheritance chain A <- B <- C <- D: dispatch through the top ------
    chain::D d;
    total += chain::top_rank(d);       // top_rank(const A&) -> D::rank at run time

    // ---- nested namespaces: call into deeply-qualified symbols -------------
    total += org::project::util::helper(2);   // -> util::helper -> net::connect
    org::project::util::Config cfg;
    total += cfg.value();                     // -> Config::value -> util::helper
    total += org::project::net::connect(8);   // C++17 compact-form namespace

    // ---- multiple inheritance: one object used through TWO interfaces ------
    zoo::Amphibian frog(5);
    int moves = zoo::make_it_walk(frog);   // via Walker&
    moves += zoo::make_it_swim(frog);      // via Swimmer&

    // ---- class template defined & used (primary) --------------------------
    cont::Wrapper<int> wi(42);
    std::string li = wi.label();           // Wrapper<int>::label -> describe<int>

    // ---- class template EXPLICIT SPECIALIZATION used ----------------------
    cont::Wrapper<bool> wb(true);
    std::string lb = wb.label();           // Wrapper<bool> specialization

    // ---- class template method calling other templates --------------------
    cont::Stack<int> st;
    st.push(1); st.push(2); st.push(3);
    std::string ss = st.summary();         // Stack<int>::summary -> combine + Wrapper

    // ---- function template + its specialization used ----------------------
    std::string d_int = cont::describe(7);     // describe<int> (primary)
    std::string d_bool = cont::describe(true); // describe<bool> (specialization)
    int t = twice(21);                         // twice<int> -> combine<int>

    // Call combine directly from non-template code so the indexer records an
    // `instantiates` edge to it (calls from inside template bodies are not
    // walked by libclang, so they would otherwise leave combine with none).
    int cmb = cont::combine(5, 6);             // -> instantiates cont::combine

    return (int)total + moves + (int)li.size() + (int)lb.size()
           + (int)ss.size() + (int)d_int.size() + (int)d_bool.size() + t + cmb;
}
