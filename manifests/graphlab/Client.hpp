#ifndef __CLIENT_HPP__
#define __CLIENT_HPP__ 

#include <string>

struct Client {
     void send(const std::string& msg) const;
};
#endif // __CLIENT_HPP__   