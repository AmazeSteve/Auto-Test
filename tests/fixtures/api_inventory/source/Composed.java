package synthetic;
import org.springframework.web.bind.annotation.*;

@RequestMapping(method = RequestMethod.POST)
@interface Operation {}

@Operation
@interface IndirectOperation {}

@RestController
class ComposedController {
    @Operation
    public Object operation() { return null; }

    @IndirectOperation
    public Object indirectOperation() { return null; }
}
