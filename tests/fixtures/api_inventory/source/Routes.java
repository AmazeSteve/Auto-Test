package synthetic;

import org.springframework.web.bind.annotation.*;
import org.springframework.stereotype.Controller;
import org.springframework.http.ResponseEntity;
import org.springframework.web.servlet.ModelAndView;

@RestController
@RequestMapping(
    path = {"/app/v1", "/app/v2"},
    params = {"version=1", "mode!=off"},
    headers = "X-Scope=read",
    consumes = "application/json",
    produces = "application/json"
)
class ApiController {
    @GetMapping(
        path = {"/users/{id}", "/members/{name}"},
        params = {"active", "!gone"},
        headers = {"Authorization=Bearer SOURCE_AUTH_CANARY", "Cookie=SOURCE_COOKIE_CANARY"},
        produces = "text/plain"
    )
    public Object list(@RequestParam(value = "id", defaultValue = ")") String id) {
        return service.get(id);
    }

    @RequestMapping(value = "/multi", method = {RequestMethod.POST, RequestMethod.GET},
        consumes = "application/xml")
    public Object multipleMethods() { return null; }

    @RequestMapping("/any")
    public Object unrestricted() { return null; }

    @PostMapping
    public void rootPost() {}

    @GetMapping(value = "/url//literal", params = "note=/*literal*/")
    public Object commentMarkers() {
        String ignored = "@GetMapping(\"/quoted-fake\") /*not a comment*/";
        return ignored;
    }

    // @GetMapping("/line-comment-fake")
    /* @PostMapping("/block-comment-fake") */
    public void ordinary() {}

    @Deprecated(since = "2")
    @DeleteMapping("/old")
    public void old() {}

    @RestController
    @RequestMapping("/nested")
    static class NestedController {
        @GetMapping("/own")
        public Object own() { return null; }
    }
}

@Controller
@RequestMapping("/views")
class ViewController {
    @ResponseBody
    @GetMapping("/json")
    public String json() { return "value"; }

    @GetMapping("/entity")
    public ResponseEntity<String> entity() { return response; }

    @GetMapping("/http-entity")
    public org.springframework.http.HttpEntity<String> httpEntity() { return response; }

    @GetMapping("/view")
    public String view() { return "/templates/home"; }

    @GetMapping("/model")
    public ModelAndView model() { return model; }

    @GetMapping("/unclear")
    public String unclear() { return service.find(); }

    @GetMapping("/object")
    public Object object() { return response; }
}

@Deprecated
@RestController
class DeprecatedController {
    @GetMapping("/deprecated-controller")
    public Object deprecatedClass() { return null; }
}

class UnregisteredController {
    @GetMapping("/unregistered")
    public Object unregistered() { return null; }
}
