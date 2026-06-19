#include "widget.hpp"

#include <utility>

namespace ui {

Widget::Widget(std::string name) : name_(std::move(name)) {}

const std::string& Widget::name() const { return name_; }

Button::Button(std::string name, int w, int h)
    : Widget(std::move(name)), w_(w), h_(h) {}

int Button::area() const { return clamp(w_ * h_, 0, 10000); }

template <typename T>
T clamp(T v, T lo, T hi) {
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

template int clamp<int>(int, int, int);

}  // namespace ui
