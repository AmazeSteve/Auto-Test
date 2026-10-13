package synthetic;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/expressions")
class Expressions {
    @GetMapping(ROUTE_CONSTANT)
    public Object constant() { return null; }

    @GetMapping("/prefix" + SUFFIX_CONSTANT)
    public Object concatenation() { return null; }

    @RequestMapping(value = "/method", method = METHOD_CONSTANT)
    public Object constantMethod() { return null; }

    @GetMapping(value = "/conditions", params = {"token=" + SECRET_CONSTANT})
    public Object constantCondition() { return "BODY_PASSWORD_CANARY"; }

    @GetMapping(value = "/password", params = {"password=SOURCE_PASSWORD_CANARY", "client_secret!=SOURCE_SECRET_CANARY"})
    public Object sensitiveConditions() { return null; }

    @GetMapping(path = "/one", value = "/two")
    public Object conflictingAliases() { return null; }

    @GetMapping("/one")
    @PostMapping("/two")
    public Object conflictingMappings() { return null; }

    @VendorMapping("/vendor")
    public Object vendor() { return null; }

    @com.vendor.GetMapping("/vendor-qualified")
    public Object vendorQualified() { return null; }

    @GetMapping(path = {"/literal", ROUTE_CONSTANT})
    public Object mixedArray() { return null; }
}

@RestController
@RequestMapping(CLASS_ROUTE)
class UnresolvedClass {
    @GetMapping("/child")
    public Object unresolvedClass() { return null; }
}
