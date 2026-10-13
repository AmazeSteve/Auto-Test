import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api")
class Items {
    @GetMapping("/items/{id}")
    public Object read() { return null; }

    @GetMapping("/unused")
    public Object unused() { return null; }

    @RequestMapping("/methodless")
    public Object ambiguous() { return null; }
}
