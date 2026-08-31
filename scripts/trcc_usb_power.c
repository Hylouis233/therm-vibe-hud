#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOCFPlugIn.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/usb/IOUSBLib.h>
#include <IOKit/usb/USB.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define TARGET_VID 0x0416
#define TARGET_PID 0x5408

static bool number_property_equals(io_service_t service, CFStringRef key,
                                   int expected) {
    CFTypeRef value = IORegistryEntryCreateCFProperty(
        service, key, kCFAllocatorDefault, 0);
    if (value == NULL || CFGetTypeID(value) != CFNumberGetTypeID()) {
        if (value != NULL) CFRelease(value);
        return false;
    }
    int actual = -1;
    bool ok = CFNumberGetValue((CFNumberRef)value, kCFNumberIntType, &actual);
    CFRelease(value);
    return ok && actual == expected;
}

static io_service_t find_target(void) {
    const char *classes[] = {"IOUSBHostDevice", "IOUSBDevice"};
    for (size_t class_index = 0;
         class_index < sizeof(classes) / sizeof(classes[0]); class_index++) {
        CFMutableDictionaryRef matching = IOServiceMatching(classes[class_index]);
        if (matching == NULL) continue;

        io_iterator_t iterator = IO_OBJECT_NULL;
        kern_return_t kr = IOServiceGetMatchingServices(
            kIOMainPortDefault, matching, &iterator);
        if (kr != KERN_SUCCESS) continue;

        io_service_t service = IO_OBJECT_NULL;
        while ((service = IOIteratorNext(iterator)) != IO_OBJECT_NULL) {
            bool matches = number_property_equals(
                               service, CFSTR(kUSBVendorID), TARGET_VID) &&
                           number_property_equals(
                               service, CFSTR(kUSBProductID), TARGET_PID);
            if (matches) {
                IOObjectRelease(iterator);
                return service;
            }
            IOObjectRelease(service);
        }
        IOObjectRelease(iterator);
    }
    return IO_OBJECT_NULL;
}

static IOReturn make_interface(io_service_t service,
                               IOUSBDeviceInterface ***device_out) {
    IOCFPlugInInterface **plugin = NULL;
    SInt32 score = 0;
    IOReturn kr = IOCreatePlugInInterfaceForService(
        service, kIOUSBDeviceUserClientTypeID, kIOCFPlugInInterfaceID,
        &plugin, &score);
    if (kr != kIOReturnSuccess || plugin == NULL) return kr;

    HRESULT hr = (*plugin)->QueryInterface(
        plugin, CFUUIDGetUUIDBytes(kIOUSBDeviceInterfaceID942),
        (LPVOID *)device_out);
    IODestroyPlugInInterface(plugin);
    return hr == S_OK ? kIOReturnSuccess : (IOReturn)hr;
}

static IOReturn print_status(IOUSBDeviceInterface **device) {
    UInt32 info = 0;
    IOReturn kr = (*device)->GetUSBDeviceInformation(device, &info);
    if (kr == kIOReturnSuccess) {
        printf("device=0416:5408 suspended=%s info=0x%08x\n",
               (info & kUSBInformationDeviceIsSuspendedMask) ? "true" : "false",
               (unsigned int)info);
    } else {
        fprintf(stderr, "GetUSBDeviceInformation failed: 0x%08x\n",
                (unsigned int)kr);
    }
    return kr;
}

static int set_suspended(IOUSBDeviceInterface **device, bool suspend) {
    IOReturn kr = (*device)->USBDeviceOpen(device);
    if (kr == kIOReturnExclusiveAccess) {
        kr = (*device)->USBDeviceOpenSeize(device);
    }
    if (kr != kIOReturnSuccess) {
        fprintf(stderr, "USBDeviceOpen failed: 0x%08x\n", (unsigned int)kr);
        return 4;
    }

    kr = (*device)->USBDeviceSuspend(device, suspend);
    if (kr != kIOReturnSuccess) {
        fprintf(stderr, "USBDeviceSuspend(%s) failed: 0x%08x\n",
                suspend ? "true" : "false", (unsigned int)kr);
        (*device)->USBDeviceClose(device);
        return 5;
    }

    kr = (*device)->USBDeviceClose(device);
    if (kr != kIOReturnSuccess) {
        fprintf(stderr, "USBDeviceClose failed: 0x%08x\n", (unsigned int)kr);
        return 6;
    }
    usleep(50000);
    return print_status(device) == kIOReturnSuccess ? 0 : 7;
}

int main(int argc, char **argv) {
    if (argc != 2 || (strcmp(argv[1], "status") != 0 &&
                      strcmp(argv[1], "suspend") != 0 &&
                      strcmp(argv[1], "resume") != 0)) {
        fprintf(stderr, "usage: %s status|suspend|resume\n", argv[0]);
        return 2;
    }

    io_service_t service = find_target();
    if (service == IO_OBJECT_NULL) {
        fprintf(stderr, "USB device 0416:5408 not found\n");
        return 3;
    }

    IOUSBDeviceInterface **device = NULL;
    IOReturn kr = make_interface(service, &device);
    IOObjectRelease(service);
    if (kr != kIOReturnSuccess || device == NULL) {
        fprintf(stderr, "Could not create IOUSBDeviceInterface: 0x%08x\n",
                (unsigned int)kr);
        return 3;
    }

    int result = 0;
    if (strcmp(argv[1], "status") == 0) {
        result = print_status(device) == kIOReturnSuccess ? 0 : 7;
    } else {
        result = set_suspended(device, strcmp(argv[1], "suspend") == 0);
    }
    (*device)->Release(device);
    return result;
}
