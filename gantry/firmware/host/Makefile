# Builds the firmware's motion code for this computer: a pretend board and its tests.
#   make test          run the motion and protocol checks
#   make virtual_gantry   the pretend board used by gantry_driver.py --port sim
CXX ?= c++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wno-unused-parameter
INC = -I../gantry_firmware
HEADERS = ../gantry_firmware/config.h ../gantry_firmware/motion.h ../gantry_firmware/protocol.h

all: virtual_gantry test_motion

virtual_gantry: virtual_gantry.cpp $(HEADERS)
	$(CXX) $(CXXFLAGS) $(INC) virtual_gantry.cpp -o $@

test_motion: test_motion.cpp $(HEADERS)
	$(CXX) $(CXXFLAGS) $(INC) test_motion.cpp -o $@

test: test_motion virtual_gantry
	./test_motion

clean:
	rm -f virtual_gantry test_motion

.PHONY: all test clean
