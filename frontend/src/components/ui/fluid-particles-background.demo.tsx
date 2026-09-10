import { FluidParticlesBackground } from "@/components/ui/fluid-particles-background";

const DemoFluidParticlesBackground = () => {
  return (
    <div className="flex h-screen w-full items-center justify-center bg-black">
      <FluidParticlesBackground>
        <div className="z-10 space-y-4 text-center lg:space-y-6">
          <h1 className="text-4xl font-bold text-white lg:text-6xl">Fluid particles</h1>
        </div>
      </FluidParticlesBackground>
    </div>
  );
};

export { DemoFluidParticlesBackground };
