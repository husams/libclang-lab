// creatures.hpp -- MULTIPLE INHERITANCE: Amphibian inherits two abstract bases.
#ifndef GRAPHLAB_CREATURES_HPP
#define GRAPHLAB_CREATURES_HPP

namespace zoo {

// Two independent abstract interfaces.
class Walker {
public:
    virtual ~Walker() = default;
    virtual int walk() = 0;     // pure virtual
};

class Swimmer {
public:
    virtual ~Swimmer() = default;
    virtual int swim() = 0;     // pure virtual
};

// Multiple inheritance: Amphibian IS-A Walker AND IS-A Swimmer.
class Amphibian : public Walker, public Swimmer {
    int stamina_;
public:
    explicit Amphibian(int stamina);
    int walk() override;        // overrides Walker::walk
    int swim() override;        // overrides Swimmer::swim
    int rest();                 // ordinary method, part of a deeper call chain
};

// Free functions that take an ABSTRACT base by reference and call its method
// (dynamic dispatch on each interface independently).
int make_it_walk(Walker& w);
int make_it_swim(Swimmer& s);

} // namespace zoo

#endif
