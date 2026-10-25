#include "widget.h"

namespace ui {

class Widget : public Base {
 public:
  void draw();
  int width() const { return w_; }
};

void Widget::draw() {
  render(this);
}

template <typename T>
T clamp_value(T v, T lo, T hi) { return v < lo ? lo : v; }

}  // namespace ui
