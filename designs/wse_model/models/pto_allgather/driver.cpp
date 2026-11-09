#include "pycircuit_system.hpp"
#include <array>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace fs = std::filesystem;
static void require(bool ok, const std::string &why) {
  if (!ok) throw std::runtime_error(why);
}
template <unsigned W> auto known(std::uint64_t value) {
  return gfsim::wire<gfsim::Bits<W>>::known(gfsim::Bits<W>{value});
}
static std::vector<std::uint8_t> load(const fs::path &path, std::size_t size) {
  std::ifstream file(path, std::ios::binary);
  require(bool(file), "cannot open " + path.string());
  std::vector<std::uint8_t> data((std::istreambuf_iterator<char>(file)), {});
  require(data.size() == size, "unexpected byte count in " + path.string());
  return data;
}
static std::uint64_t word(const std::vector<std::uint8_t> &data, std::size_t offset) {
  std::uint64_t value = 0;
  for (unsigned i = 0; i < 8; ++i) value |= std::uint64_t(data[offset+i]) << (8*i);
  return value;
}
struct Request {
  unsigned kind=0, node=0, address=0;
  std::uint64_t data=0;
  std::uint32_t permit=0xffffffff;
};
struct Snapshot {
  std::uint64_t data=0;
  std::uint32_t counted_b=0, counted_c=0;
  unsigned identity=0, stage=0, descriptor=0, offset=0;
  bool done=false;
};
struct Simulation {
  pyc_dut dut{1};
  pyc_dut::Inputs inputs{};
  std::uint64_t steps=0, cycles=0, source_read_bytes=0, destination_write_bytes=0;
  int stall_descriptor=-1, stall_node=-1, stall_cycles=0, stalled=0;
  bool stall_started=false;
  std::vector<Snapshot> stall_samples;

  static auto pack(const Request &request) {
    gfsim::Bits<120> bits{};
    auto put=[&](unsigned offset,unsigned width,std::uint64_t value) {
      for(unsigned bit=0;bit<width;++bit)
        if((value>>bit)&1) bits=gfsim::detail::setBit(bits,offset+bit);
    };
    put(0,32,request.permit); put(32,64,request.data);
    put(96,16,request.address); put(112,5,request.node); put(117,3,request.kind);
    return gfsim::FourState<120>::known(bits);
  }
  Snapshot sample() {
    auto outputs=dut.sample();
    const auto available=outputs.pyc_5f5f7079635f76616c69645f6f75745f30;
    require(available.isFullyKnown()&&available.value().toBool(),
            "DUT result is not available");
    auto bits=outputs.result.packed();
    auto get=[&](unsigned offset,unsigned width) {
      std::uint64_t value=0;
      for(unsigned bit=0;bit<width;++bit) {
        require(bits.knownMask().bit(offset+bit)&&!bits.zMask().bit(offset+bit),"unknown DUT result");
        value|=std::uint64_t(bits.value().bit(offset+bit))<<bit;
      }
      return value;
    };
    Snapshot out;
    out.done=get(0,1);out.offset=get(1,16);out.descriptor=get(17,9);
    out.stage=get(26,4);out.identity=get(30,10);out.counted_c=get(40,32);
    out.counted_b=get(72,32);out.data=get(104,64);
    return out;
  }
  void drive(const Request &request, unsigned clock) {
    inputs.host=decltype(inputs.host)::fromPacked(pack(request));
    inputs.pyc_7079635f636c6b=known<1>(clock);
    inputs.pyc_7079635f727374=known<1>(0);
    dut.drive(inputs);
  }
  Simulation() {
    drive({},0);dut.system().Build();dut.system().Reset();
    require(dut.system().state()==gfsim::SimSystemState::Ready,"DUT initialization failed");
  }
  void step() {
    auto result=dut.system().Step();++steps;
    if(result!=gfsim::SimStepResult::Running) {
      auto failure=dut.system().failureInfo();
      throw std::runtime_error("checked pyCircuit step failed: "+std::string(failure.code)+" "+std::string(failure.message)+" source="+std::string(failure.sourceJson));
    }
  }
  Snapshot cycle(Request request, bool running=false) {
    drive(request,0);step();auto before=sample();
    if(running && !stall_started && before.stage==3 &&
       int(before.descriptor)==stall_descriptor && before.offset==8) {
      stall_started=true;
    }
    bool block=running&&stall_started&&stalled<stall_cycles;
    if(block) {
      require(before.stage==3&&int(before.descriptor)==stall_descriptor&&before.offset==8,
              "DUT changed descriptor/offset while landing was blocked");
      request.permit&=~(std::uint32_t(1)<<stall_node);
      ++stalled;stall_samples.push_back(before);
    }
    drive(request,1);step();++cycles;
    if(running&&before.stage==2) source_read_bytes+=8;
    if(running&&before.stage==3&&!block)
      destination_write_bytes+=(before.descriptor<256?8:4)*8;
    auto observed=sample();
    if(cycles%16384==0)
      std::cerr<<"PROGRESS cycles="<<cycles<<" stage="<<observed.stage
               <<" descriptor="<<observed.descriptor<<" offset="<<observed.offset<<'\n';
    return observed;
  }
};

int main(int argc,char **argv) {
  fs::path receipt;
  try {
    require(argc>=5,"usage: payload_runner INPUT.bin COMMANDS.bin OUTPUT.bin RECEIPT.json [--stall-descriptor N --stall-node N --stall-cycles N]");
    fs::path input_path=argv[1], command_path=argv[2], output_path=argv[3];receipt=argv[4];
    require(!fs::exists(output_path),"refusing to replace existing payload output");
    auto input=load(input_path,32*1536);auto commands=load(command_path,512*48);
    Simulation simulation;
    for(int arg=5;arg<argc;arg+=2) {
      require(arg+1<argc,"missing option value");std::string key=argv[arg];int value=std::stoi(argv[arg+1]);
      if(key=="--stall-descriptor")simulation.stall_descriptor=value;
      else if(key=="--stall-node")simulation.stall_node=value;
      else if(key=="--stall-cycles")simulation.stall_cycles=value;
      else throw std::runtime_error("unknown option "+key);
    }
    if(simulation.stall_descriptor>=0)
      require(simulation.stall_descriptor<512&&simulation.stall_node>=0&&simulation.stall_node<32&&simulation.stall_cycles>0,"invalid stall configuration");
    std::cerr<<"BEGIN initializing 32 node memories\n";
    for(unsigned node=0;node<32;++node)
      for(unsigned address=0;address<1536;address+=8)
        simulation.cycle({1,node,address,word(input,node*1536+address)});
    std::cerr<<"BEGIN loading 512 runtime descriptors\n";
    for(unsigned address=0;address<24576;address+=8)
      simulation.cycle({2,0,address,word(commands,address)});
    std::cerr<<"BEGIN generated AllGather execution\n";
    simulation.cycle({3});
    auto start_cycles=simulation.cycles;
    Snapshot state;
    for(unsigned bound=0;bound<500000;++bound) {
      state=simulation.cycle({},true);
      if(state.done)break;
    }
    require(state.done,"DUT did not complete within explicit 500000-cycle bound");
    require(simulation.stall_descriptor<0||simulation.stalled==simulation.stall_cycles,"requested landing stall was not observed");
    auto run_cycles=simulation.cycles-start_cycles;
    std::cerr<<"BEGIN reading back 32 complete 49152-byte outputs\n";
    std::vector<std::uint8_t> output;output.reserve(32*49152);
    std::array<Snapshot,32> accounts;
    for(unsigned node=0;node<32;++node) {
      for(unsigned address=16384;address<65536;address+=8) {
        auto observed=simulation.cycle({4,node,address});
        require(observed.done&&observed.counted_b==10752&&observed.counted_c==36864,
                "per-node receive accounting mismatch");
        accounts[node]=observed;
        for(unsigned byte=0;byte<8;++byte)output.push_back((observed.data>>(byte*8))&255);
      }
    }
    fs::path temporary=output_path;temporary+=".tmp";
    {std::ofstream file(temporary,std::ios::binary);require(bool(file),"cannot create output");
      file.write(reinterpret_cast<const char*>(output.data()),output.size());require(bool(file),"cannot write output");}
    fs::rename(temporary,output_path);
    std::ofstream report(receipt);require(bool(report),"cannot create receipt");
    report<<"{\n  \"status\":\"EXECUTED\",\n  \"backend\":\"cpp\",\n  \"workers\":1,\n"
          <<"  \"members\":32,\n  \"carrier_nodes\":40,\n  \"descriptors\":512,\n"
          <<"  \"steps\":"<<simulation.steps<<",\n  \"cycles\":"<<simulation.cycles<<",\n"
          <<"  \"execution_cycles\":"<<run_cycles<<",\n  \"source_read_bytes\":"<<simulation.source_read_bytes<<",\n"
          <<"  \"destination_write_bytes\":"<<simulation.destination_write_bytes<<",\n"
          <<"  \"input_bytes\":"<<input.size()<<",\n  \"output_bytes\":"<<output.size()<<",\n"
          <<"  \"stall_cycles\":"<<simulation.stalled<<",\n  \"accounts\":[";
    for(unsigned node=0;node<32;++node) {
      if(node)report<<',';
      report<<"{\"node\":"<<node<<",\"counted_b\":"<<accounts[node].counted_b
            <<",\"counted_c\":"<<accounts[node].counted_c<<",\"last_identity\":"<<accounts[node].identity<<'}';
    }
    report<<"],\n  \"stall_observations\":[";
    for(unsigned i=0;i<simulation.stall_samples.size();++i) {
      if(i)report<<',';auto &s=simulation.stall_samples[i];
      report<<"{\"descriptor\":"<<s.descriptor<<",\"offset\":"<<s.offset<<",\"stage\":"<<s.stage<<'}';
    }
    report<<"]\n}\n";
    std::cout<<"EXECUTED 32-node payload, "<<output.size()<<" output bytes, "<<run_cycles<<" functional execution cycles\n";
    return 0;
  } catch(const std::exception &error) {
    std::cerr<<error.what()<<'\n';
    if(!receipt.empty()) {std::ofstream report(receipt);report<<"{\"status\":\"FAILED\"}\n";}
    return 1;
  }
}
