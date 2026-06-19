#ifndef WIDGET_HPP
#define WIDGET_HPP

#include <string>

// Versioned C++ library (trailing dir "v1.4.0") used to exercise cidx version
// detection + C++ symbol/graph extraction on a real C++ directory.
namespace ui {

class Widget {
public:
    explicit Widget(std::string name);
    virtual ~Widget() = default;
    virtual int area() const = 0;
    const std::string& name() const;

private:
    std::string name_;
};

class Button : public Widget {
public:
    Button(std::string name, int w, int h);
    int area() const override;

private:
    int w_;
    int h_;
};

template <typename T>
T clamp(T v, T lo, T hi);

}  // namespace ui

#endif  // WIDGET_HPP
