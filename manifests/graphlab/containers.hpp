// containers.hpp -- FUNCTION TEMPLATES, CLASS TEMPLATES, and SPECIALIZATIONS.
//
// Covers:
//   * function template defined + (explicitly) specialized
//   * class template defined + (explicitly) specialized
//   * a class-template method that calls ANOTHER template (a function template
//     and another class template's method)
#ifndef GRAPHLAB_CONTAINERS_HPP
#define GRAPHLAB_CONTAINERS_HPP

#include <string>

namespace cont {

// ---- function template: PRIMARY definition -------------------------------- //
template <class T>
std::string describe(const T& v) {
    return std::string("value=") + std::to_string(v);
}

// ---- function template: EXPLICIT SPECIALIZATION for bool ------------------- //
// Defined here, used by Wrapper<bool>::label() and by main.
template <>
inline std::string describe<bool>(const bool& v) {
    return std::string("flag=") + (v ? "true" : "false");
}

// ---- a second function template, used by a class-template method ----------- //
template <class T>
T combine(T a, T b) {
    return a + b;
}

// ---- class template: PRIMARY definition ----------------------------------- //
template <class T>
class Wrapper {
    T value_;
public:
    explicit Wrapper(T v) : value_(v) {}
    const T& get() const { return value_; }

    // class-template METHOD that calls a FUNCTION TEMPLATE (describe<T>).
    std::string label() const {
        return describe(value_);   // -> describe<T> (or its specialization)
    }
};

// ---- class template: EXPLICIT SPECIALIZATION for bool ---------------------- //
// Its label() calls the describe<bool> specialization.
template <>
class Wrapper<bool> {
    bool value_;
public:
    explicit Wrapper(bool v) : value_(v) {}
    bool get() const { return value_; }
    std::string label() const {
        return std::string("bool-wrapper:") + describe(value_);
    }
};

// ---- a second class template whose method calls ANOTHER TYPE'S template ---- //
template <class T>
class Stack {
    T data_[8];
    int size_ = 0;
public:
    void push(const T& v) { if (size_ < 8) data_[size_++] = v; }
    int size() const { return size_; }

    // METHOD calling another class template's method (Wrapper<T>::label) AND a
    // function template (combine<T>): cross-template calls from inside a template.
    std::string summary() const {
        T acc = data_[0];
        for (int i = 1; i < size_; ++i)
            acc = combine(acc, data_[i]);     // function-template call
        Wrapper<T> w(acc);
        return w.label();                     // class-template method call
    }
};

} // namespace cont

#endif
